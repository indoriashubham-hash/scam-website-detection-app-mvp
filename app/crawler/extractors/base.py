"""Extractor base types. Extractors are tiny pure functions that read an
``ExtractContext`` and return an ``ExtractorResult``.

Design choices:
- BeautifulSoup is parsed once and shared. lxml is fast enough and the extractor
  surface is small, so we don't need an incremental parser.
- Extractors can propose evidence (by kind, severity, details). The pipeline writes
  them later to keep extractors free of DB coupling.
- Extractors should not raise on malformed input; return empty results instead.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from app.crawler.urls import ParsedUrl, normalize_url


@dataclass(slots=True)
class ProposedEvidence:
    kind: str
    severity: str                # "info"|"low"|"medium"|"high"|"critical"
    summary: str
    confidence: float = 0.5
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProposedLink:
    href: str
    rel: str | None = None
    anchor_text: str | None = None


@dataclass(slots=True)
class ExtractorResult:
    # Merged into pages.extracted
    extracted: dict[str, Any] = field(default_factory=dict)
    # Proposed evidence rows (written by the pipeline)
    evidence: list[ProposedEvidence] = field(default_factory=list)
    # Structured writes
    forms: list[dict] = field(default_factory=list)
    links: list[ProposedLink] = field(default_factory=list)


@dataclass(slots=True)
class ExtractContext:
    page_url: ParsedUrl
    final_url: ParsedUrl
    html: str
    title: str
    status: int
    mime: str | None
    cookies: list[dict]
    console_errors: list[str]
    soup: BeautifulSoup
    # running merged output from previous extractors in the pipeline
    extracted: dict[str, Any] = field(default_factory=dict)


Extractor = Callable[[ExtractContext], ExtractorResult]


def make_context(
    *,
    page_url: str,
    final_url: str,
    html: str,
    title: str,
    status: int,
    mime: str | None,
    cookies: list[dict],
    console_errors: list[str],
) -> ExtractContext:
    soup = BeautifulSoup(html or "", "lxml")
    return ExtractContext(
        page_url=normalize_url(page_url),
        final_url=normalize_url(final_url or page_url),
        html=html or "",
        title=title,
        status=status,
        mime=mime,
        cookies=cookies,
        console_errors=console_errors,
        soup=soup,
    )


def run_pipeline(ctx: ExtractContext, extractors: Iterable[Extractor]) -> ExtractorResult:
    """Run extractors in order, merging outputs into a single ExtractorResult."""
    combined = ExtractorResult()
    for fn in extractors:
        try:
            r = fn(ctx)
        except Exception as e:                  # never let one extractor kill the pipeline
            combined.evidence.append(
                ProposedEvidence(
                    kind="crawl.no_signal",     # best-available known kind; vocabulary strict
                    severity="info",
                    summary=f"extractor {fn.__name__} crashed",
                    details={"error": str(e)[:400]},
                )
            )
            continue
        combined.extracted.update(r.extracted)
        combined.evidence.extend(r.evidence)
        combined.forms.extend(r.forms)
        combined.links.extend(r.links)
        # let downstream extractors see what's been collected so far
        ctx.extracted.update(r.extracted)
    return combined
