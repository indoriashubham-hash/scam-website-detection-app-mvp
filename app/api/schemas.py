"""Pydantic schemas for the API surface."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class InvestigationCreate(BaseModel):
    url: HttpUrl = Field(..., description="The URL to investigate. Must be http(s).")
    # Optional: user-supplied Anthropic API key, used for Track 1 (narrative)
    # during this crawl. If omitted, the server falls back to the
    # ANTHROPIC_API_KEY env var; if that is also unset, the LLM step is
    # skipped and the user sees the deterministic summary only.
    #
    # Security posture: the key is passed through to the RQ job payload and
    # lives in Redis only until the job completes. It is never persisted to
    # Postgres. Callers should send this over HTTPS in any non-local
    # deployment.
    anthropic_api_key: str | None = Field(default=None, repr=False)


class DeepReviewRequest(BaseModel):
    # Same BYOK pattern as InvestigationCreate, for the Track 2 endpoint.
    # The key is used once for the single vision call and not stored.
    anthropic_api_key: str | None = Field(default=None, repr=False)


class InvestigationSummary(BaseModel):
    id: uuid.UUID
    input_url: str
    normalized_origin: str
    status: str
    risk_band: str | None
    confidence: float | None
    # Human-readable narrative written by the Risk Aggregator. Null until the
    # investigation completes.
    summary: str | None = None
    # Top findings the aggregator ranked as most responsible for the band.
    # Each entry: {kind, severity, confidence, summary, score}.
    findings: list[dict] = Field(default_factory=list)
    # LLM-produced plain-English narrative. Null if LLM was skipped (no API
    # key) or its output was rejected by validation. Consumers should fall
    # back to `summary` when this is null.
    narrative: dict | None = None
    created_at: datetime
    completed_at: datetime | None


class EvidenceRow(BaseModel):
    id: uuid.UUID
    analyzer: str
    kind: str
    severity: str
    confidence: float
    summary: str
    details: dict
    screenshot_url: str | None
    page_id: uuid.UUID | None
    created_at: datetime


class PageRow(BaseModel):
    id: uuid.UUID
    url: str
    final_url: str | None
    http_status: int | None
    title: str | None
    lang: str | None
    word_count: int | None
    screenshot_url: str | None
    atf_screenshot_url: str | None
    extracted: dict
    # Role flags — derived from pages.extracted, surfaced at the top level so
    # the UI doesn't need to look inside the JSON blob to know which row is
    # the user-submitted URL.
    is_seed: bool = False
    is_homepage_compare: bool = False


class InvestigationDetail(InvestigationSummary):
    pages: list[PageRow]
    evidence: list[EvidenceRow]
