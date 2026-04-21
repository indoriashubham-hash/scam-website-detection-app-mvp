"""FastAPI app — thin. Create an investigation, read it back.

Also serves a minimal static web UI from ``app/web/static``. The UI is plain
HTML + vanilla JS — no build step, no external CDN. Two views:

* ``/`` — URL submission form + list of recent investigations.
* ``/i/{id}`` — investigation detail: verdict card, findings, pages, and a
  Deep Review tab that triggers the Track 2 LLM on demand.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import redis
import structlog
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from rq import Queue
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.schemas import (
    DeepReviewRequest,
    EvidenceRow,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationSummary,
    PageRow,
)
from app.config import settings
from app.crawler.urls import is_private_target, normalize_url
from app.db import get_session
from app.models import Investigation
from app.reporter import deep_review as run_deep_review

log = structlog.get_logger(__name__)

app = FastAPI(title="Website Risk Investigator", version="0.1.0")

_redis = redis.Redis.from_url(settings().redis_url)
_queue = Queue("wri", connection=_redis, default_timeout=900)

# Static web UI. The ``/ui/`` mount serves CSS + JS; the two HTML entry pages
# are returned by explicit routes below so they also work without trailing
# slashes and so the detail view can accept a UUID segment.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
if _STATIC_DIR.exists():
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR), name="ui")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# --- UI entry points --------------------------------------------------------
# These must come before the `/investigations/...` API routes don't actually
# clash, but keep them near the top so the surface is obvious.


@app.get("/", include_in_schema=False)
async def ui_home():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/i/{inv_id}", include_in_schema=False)
async def ui_investigation(inv_id: uuid.UUID):
    # Same HTML for every investigation page; the JS reads the UUID from the
    # URL and fetches /investigations/{id}. No server-side template needed.
    return FileResponse(_STATIC_DIR / "investigation.html")


# --- List endpoint for the home page ----------------------------------------


@app.get("/investigations", response_model=list[InvestigationSummary])
async def list_investigations(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> list[InvestigationSummary]:
    """Recent investigations, newest first. Backs the home-page list."""
    limit = max(1, min(100, limit))
    stmt = select(Investigation).order_by(desc(Investigation.created_at)).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        InvestigationSummary(
            id=r.id,
            input_url=r.input_url,
            normalized_origin=r.normalized_origin,
            status=r.status,
            risk_band=r.risk_band,
            confidence=float(r.confidence) if r.confidence is not None else None,
            summary=r.summary,
            findings=list(r.findings or []),
            narrative=r.narrative,
            created_at=r.created_at,
            completed_at=r.completed_at,
        )
        for r in rows
    ]


@app.post("/investigations", response_model=InvestigationSummary, status_code=status.HTTP_201_CREATED)
async def create_investigation(
    payload: InvestigationCreate,
    session: AsyncSession = Depends(get_session),
) -> InvestigationSummary:
    try:
        parsed = normalize_url(str(payload.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid URL: {e}") from e
    if is_private_target(parsed.host):
        raise HTTPException(status_code=400, detail="refusing to investigate private/internal host")

    inv = Investigation(
        input_url=str(payload.url),
        normalized_origin=parsed.origin,
        status="queued",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(inv)
    await session.flush([inv])

    # Pass the user-supplied key (if any) through as a kwarg. The worker's
    # investigate() treats it as optional and falls back to the env var when
    # absent. We deliberately keep the key out of the database — it lives in
    # the RQ job payload in Redis only until the crawl finishes.
    _queue.enqueue(
        "app.worker.tasks.investigate",
        str(inv.id),
        str(payload.url),
        anthropic_api_key=payload.anthropic_api_key,
        job_id=f"inv-{inv.id}",
    )

    return InvestigationSummary(
        id=inv.id,
        input_url=inv.input_url,
        normalized_origin=inv.normalized_origin,
        status=inv.status,
        risk_band=inv.risk_band,
        confidence=float(inv.confidence) if inv.confidence is not None else None,
        summary=inv.summary,
        findings=list(inv.findings or []),
        narrative=inv.narrative,
        created_at=inv.created_at,
        completed_at=inv.completed_at,
    )


@app.get("/investigations/{inv_id}", response_model=InvestigationDetail)
async def get_investigation(
    inv_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> InvestigationDetail:
    stmt = (
        select(Investigation)
        .where(Investigation.id == inv_id)
        .options(
            selectinload(Investigation.pages),
            selectinload(Investigation.evidence),
        )
    )
    inv = (await session.execute(stmt)).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "investigation not found")

    base = settings().s3_public_base

    pages = [
        PageRow(
            id=p.id,
            url=p.url,
            final_url=p.final_url,
            http_status=p.http_status,
            title=p.title,
            lang=p.lang,
            word_count=p.word_count,
            screenshot_url=(f"{base}/{p.screenshot_key}" if p.screenshot_key else None),
            atf_screenshot_url=(f"{base}/{p.ato_screenshot_key}" if p.ato_screenshot_key else None),
            extracted=p.extracted or {},
            is_seed=bool((p.extracted or {}).get("is_seed")),
            is_homepage_compare=bool((p.extracted or {}).get("is_homepage_compare")),
        )
        for p in inv.pages
    ]
    # Sort the seed page first, then the homepage_compare page, then the rest
    # in the order they were crawled. The seed is the investigation subject —
    # whatever consumes this API (UI, report generator, reviewer) should see
    # it before anything else.
    pages.sort(
        key=lambda r: (0 if r.is_seed else 1 if r.is_homepage_compare else 2)
    )
    evidence = [
        EvidenceRow(
            id=e.id,
            analyzer=e.analyzer,
            kind=e.kind,
            severity=e.severity,
            confidence=float(e.confidence),
            summary=e.summary,
            details=e.details or {},
            screenshot_url=(f"{base}/{e.screenshot_key}" if e.screenshot_key else None),
            page_id=e.page_id,
            created_at=e.created_at,
        )
        for e in inv.evidence
    ]

    return InvestigationDetail(
        id=inv.id,
        input_url=inv.input_url,
        normalized_origin=inv.normalized_origin,
        status=inv.status,
        risk_band=inv.risk_band,
        confidence=float(inv.confidence) if inv.confidence is not None else None,
        summary=inv.summary,
        findings=list(inv.findings or []),
        narrative=inv.narrative,
        created_at=inv.created_at,
        completed_at=inv.completed_at,
        pages=pages,
        evidence=evidence,
    )


@app.post("/investigations/{inv_id}/deep-review")
async def trigger_deep_review(
    inv_id: uuid.UUID,
    force: bool = False,
    payload: DeepReviewRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Run (or return the cached) deep LLM review for an investigation.

    This is the Track 2 endpoint. It's on-demand because the LLM call is
    expensive (vision + long context). Results are cached in
    ``investigations.deep_review``; pass ``?force=true`` to regenerate.

    The request body is optional. When present with ``anthropic_api_key``, the
    user-supplied key is used for this one call; otherwise the server's
    ANTHROPIC_API_KEY env var is used (or the call is skipped entirely if
    neither is available).
    """
    inv = await session.get(Investigation, inv_id)
    if inv is None:
        raise HTTPException(404, "investigation not found")
    if inv.status != "done":
        raise HTTPException(
            409,
            f"investigation not ready (status={inv.status}); deep review requires a completed crawl",
        )

    if inv.deep_review is not None and not force:
        return {"deep_review": inv.deep_review, "cached": True}

    api_key = payload.anthropic_api_key if payload is not None else None
    result = await run_deep_review(inv_id, session, api_key=api_key)
    if result is None:
        raise HTTPException(
            503,
            "deep review unavailable (LLM not configured or output could not be validated)",
        )
    inv.deep_review = result.to_dict()
    await session.flush([inv])
    return {"deep_review": inv.deep_review, "cached": False}


@app.get("/investigations/{inv_id}/deep-review")
async def get_deep_review(
    inv_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the cached deep review, or 404 if none has been generated yet."""
    inv = await session.get(Investigation, inv_id)
    if inv is None:
        raise HTTPException(404, "investigation not found")
    if inv.deep_review is None:
        raise HTTPException(404, "no deep review cached — POST to this URL to generate one")
    return {"deep_review": inv.deep_review, "cached": True}
