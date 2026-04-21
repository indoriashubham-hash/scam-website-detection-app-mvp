"""RQ task entrypoints.

RQ calls these synchronously; we bridge to asyncio with ``asyncio.run``. Each call
opens a Playwright browser and closes it at the end — fine for v1 (investigations are
minutes long, not milliseconds). We can switch to a long-lived browser + gRPC worker
in a later milestone if warm-start matters.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from app.aggregator import aggregate
from app.crawler.pipeline import CrawlPipeline
from app.crawler.renderer import Renderer
from app.db import session_scope
from app.models import Evidence, Investigation
from app.reporter import write_report

log = structlog.get_logger(__name__)


def investigate(
    investigation_id: str,
    input_url: str,
    anthropic_api_key: str | None = None,
) -> str:
    """Blocking RQ entrypoint.

    ``anthropic_api_key`` is optional; when provided it's used for the
    post-crawl Track 1 narrative. This keeps the key confined to the single
    job — it never lands in Postgres. If ``None``, the reporter falls back to
    the server's ANTHROPIC_API_KEY env var (or skips the LLM step).
    """
    return asyncio.run(
        _investigate_async(
            uuid.UUID(investigation_id),
            input_url,
            anthropic_api_key=anthropic_api_key,
        )
    )


async def _investigate_async(
    investigation_id: uuid.UUID,
    input_url: str,
    *,
    anthropic_api_key: str | None = None,
) -> str:
    log.info("worker.investigate.start", investigation_id=str(investigation_id), url=input_url)

    # mark crawling
    async with session_scope() as s:
        inv = await s.get(Investigation, investigation_id)
        if inv is None:
            log.warning("worker.investigate.missing", id=str(investigation_id))
            return "missing"
        inv.status = "crawling"

    renderer = Renderer()
    await renderer.start()
    try:
        async with session_scope() as s:
            async with CrawlPipeline(
                session=s,
                investigation_id=investigation_id,
                seed_url=input_url,
                renderer=renderer,
            ) as pipe:
                await pipe.run()

        # Crawl finished; run the deterministic aggregator over the evidence we
        # wrote and persist the verdict before marking done. Order matters: if
        # this step fails we want the investigation to end in "failed" with a
        # real error, not silently stuck in "analyzing".
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(Evidence).where(Evidence.investigation_id == investigation_id)
                )
            ).scalars().all()
            verdict = aggregate(rows)
            log.info(
                "worker.investigate.verdict",
                investigation_id=str(investigation_id),
                risk_band=verdict.risk_band,
                confidence=verdict.confidence,
                score=verdict.score,
                reason=verdict.reason,
                finding_count=len(verdict.findings),
            )

            # LLM narrative is best-effort. If it's missing the verdict still
            # stands; the API just returns narrative=null and the UI shows the
            # deterministic summary. The call itself never raises — see
            # app/reporter/writer.py for the defense layers.
            narrative = write_report(verdict, input_url, api_key=anthropic_api_key)

            inv = await s.get(Investigation, investigation_id)
            if inv is not None:
                inv.risk_band = verdict.risk_band
                inv.confidence = verdict.confidence
                inv.summary = verdict.summary
                inv.findings = [f.to_dict() for f in verdict.findings]
                inv.narrative = narrative.to_dict() if narrative is not None else None
                inv.status = "done"
                inv.completed_at = datetime.now(timezone.utc)
        log.info("worker.investigate.done", investigation_id=str(investigation_id))
        return "done"
    except Exception as e:  # noqa: BLE001
        log.exception("worker.investigate.failed", err=str(e))
        async with session_scope() as s:
            inv = await s.get(Investigation, investigation_id)
            if inv is not None:
                inv.status = "failed"
                inv.error = str(e)[:2000]
                inv.completed_at = datetime.now(timezone.utc)
        return "failed"
    finally:
        await renderer.stop()
