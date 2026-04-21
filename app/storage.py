"""Thin S3/MinIO wrapper. Keys are deterministic: ``<investigation_id>/<page_id>/<name>``.

Why sync boto3 here instead of aiobotocore? Uploads run inside the worker, which is a
blocking RQ worker; simpler is better. Swap for aiobotocore if we ever need concurrency
within a single page.
"""
from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from app.config import settings


@dataclass(slots=True, frozen=True)
class StorageKey:
    key: str

    @property
    def public_url(self) -> str:
        return f"{settings().s3_public_base}/{self.key}"


class Storage:
    """Light wrapper around boto3 s3 client pointed at MinIO."""

    def __init__(self) -> None:
        cfg = settings()
        self.bucket = cfg.s3_bucket
        self._client: BaseClient = boto3.client(
            "s3",
            endpoint_url=cfg.s3_endpoint,
            aws_access_key_id=cfg.s3_access_key,
            aws_secret_access_key=cfg.s3_secret_key,
            region_name="us-east-1",
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def put_bytes(
        self,
        investigation_id: uuid.UUID,
        page_id: uuid.UUID | None,
        name: str,
        data: bytes,
        content_type: str,
    ) -> StorageKey:
        key = f"{investigation_id}/{page_id or 'inv'}/{name}"
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=io.BytesIO(data),
            ContentType=content_type,
            ContentLength=len(data),
        )
        return StorageKey(key=key)

    def put_file(
        self,
        investigation_id: uuid.UUID,
        page_id: uuid.UUID | None,
        name: str,
        path: str,
        content_type: str,
    ) -> StorageKey:
        key = f"{investigation_id}/{page_id or 'inv'}/{name}"
        with open(path, "rb") as fh:
            self._client.put_object(
                Bucket=self.bucket, Key=key, Body=fh, ContentType=content_type
            )
        return StorageKey(key=key)

    def get_bytes(self, key: str) -> bytes | None:
        """Read an object back from the bucket. Returns None if missing.

        Used by the Deep Reviewer to fetch screenshots for the vision-capable
        LLM call. Keep this sync — callers run it in a threadpool.
        """
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except ClientError:
            return None


_storage: Storage | None = None


def get_storage() -> Storage:
    """Process-wide singleton."""
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
