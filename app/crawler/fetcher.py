"""Tier 1 static fetcher (httpx). Used for cheap HEAD checks and for the initial
redirect resolution; rendered pages go through the Playwright renderer."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class StaticFetchResult:
    final_url: str
    status: int
    mime: str | None
    redirect_chain: list[str]
    content_length: int | None
    ok: bool


async def probe(client: httpx.AsyncClient, url: str) -> StaticFetchResult:
    """Issue a GET with a small range to cheaply discover status+redirects.

    We use GET-with-Range instead of HEAD because many sites misbehave on HEAD.
    """
    try:
        r = await client.get(
            url,
            headers={"Range": "bytes=0-1024"},
            timeout=10.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        log.info("fetcher.probe.err", url=url, err=str(e))
        return StaticFetchResult(
            final_url=url, status=0, mime=None, redirect_chain=[], content_length=None, ok=False
        )
    chain = [str(h.url) for h in r.history] + [str(r.url)]
    cl = int(r.headers.get("content-length")) if r.headers.get("content-length", "").isdigit() else None
    return StaticFetchResult(
        final_url=str(r.url),
        status=r.status_code,
        mime=(r.headers.get("content-type") or "").split(";")[0].strip() or None,
        redirect_chain=chain,
        content_length=cl,
        ok=200 <= r.status_code < 400,
    )
