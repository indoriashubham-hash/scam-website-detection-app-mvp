"""Declared vs detected language; emits a weak evidence row when they mismatch."""
from __future__ import annotations

from app.crawler.extractors.base import ExtractContext, ExtractorResult, ProposedEvidence


def extract_language(ctx: ExtractContext) -> ExtractorResult:
    declared = None
    html_tag = ctx.soup.find("html")
    if html_tag and html_tag.get("lang"):
        declared = html_tag["lang"].split("-")[0].lower()

    detected = None
    text = ctx.extracted.get("readable_text") or ""
    if text and len(text) > 80:
        try:
            from langdetect import DetectorFactory, detect

            DetectorFactory.seed = 0
            detected = detect(text)
        except Exception:
            detected = None

    evidence = []
    if declared and detected and declared != detected:
        evidence.append(
            ProposedEvidence(
                kind="crawl.language_mismatch",
                severity="low",
                summary=f"<html lang='{declared}'> but body detected as '{detected}'",
                details={"declared": declared, "detected": detected},
                confidence=0.6,
            )
        )

    return ExtractorResult(
        extracted={"lang_declared": declared, "lang_detected": detected},
        evidence=evidence,
    )
