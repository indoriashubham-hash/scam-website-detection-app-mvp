"""Evidence emitter — the single entrypoint for writing `evidence` rows.

The `kind` string is a controlled vocabulary declared in `app.crawler.vocabulary`.
Emitting an unknown kind is a hard error in dev/tests and a logged warning in prod;
this keeps the reporter's narrative templates sane as analyzers evolve.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.vocabulary import KNOWN_KINDS, Severity
from app.models import Evidence

log = structlog.get_logger(__name__)

_STRICT = os.getenv("WRI_STRICT_VOCAB", "1") == "1"


@dataclass(slots=True)
class EmittedEvidence:
    id: uuid.UUID
    kind: str
    severity: str
    summary: str


class EvidenceEmitter:
    """Writes a single evidence row. Use one per investigation."""

    def __init__(self, session: AsyncSession, investigation_id: uuid.UUID, analyzer: str) -> None:
        self.session = session
        self.investigation_id = investigation_id
        self.analyzer = analyzer

    async def emit(
        self,
        *,
        kind: str,
        severity: Severity,
        summary: str,
        confidence: float = 0.5,
        details: dict[str, Any] | None = None,
        page_id: uuid.UUID | None = None,
        screenshot_key: str | None = None,
    ) -> EmittedEvidence:
        if kind not in KNOWN_KINDS:
            msg = f"unknown evidence kind: {kind!r} (analyzer={self.analyzer})"
            if _STRICT:
                raise ValueError(msg)
            log.warning("evidence.unknown_kind", kind=kind, analyzer=self.analyzer)

        row = Evidence(
            investigation_id=self.investigation_id,
            analyzer=self.analyzer,
            kind=kind,
            severity=severity.value if isinstance(severity, Severity) else severity,
            confidence=confidence,
            summary=summary,
            details=details or {},
            page_id=page_id,
            screenshot_key=screenshot_key,
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(row)
        await self.session.flush([row])
        log.info(
            "evidence.emitted",
            kind=kind,
            severity=row.severity,
            analyzer=self.analyzer,
            summary=summary,
        )
        return EmittedEvidence(id=row.id, kind=kind, severity=row.severity, summary=summary)
