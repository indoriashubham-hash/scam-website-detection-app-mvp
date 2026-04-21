"""Report Writer — turns a deterministic Verdict into plain-English narrative.

The aggregator decides the verdict. This module only *translates* — and only
when an Anthropic API key is configured. If anything goes wrong (missing key,
network failure, schema violation, hallucination detected), the caller falls
back to the aggregator's template summary. The product must never stall or
produce garbage because of an LLM problem.
"""
from __future__ import annotations

from app.reporter.deep import deep_review
from app.reporter.narrative import (
    DeepReview,
    Narrative,
    SignalExplanation,
    SourcedObservation,
)
from app.reporter.writer import write_report

__all__ = [
    "DeepReview",
    "Narrative",
    "SignalExplanation",
    "SourcedObservation",
    "deep_review",
    "write_report",
]
