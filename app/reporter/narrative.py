"""Structured narrative returned by the LLM report writer.

Intentionally small: the LLM fills these fields and nothing else. Keep it
JSON-serializable so it stores directly in the ``investigations.narrative``
JSONB column and flows straight to the API/UI.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SignalExplanation:
    kind: str            # must match a finding.kind provided to the LLM
    plain_english: str   # lay-reader rewrite of the finding summary

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourcedClaim:
    """One sourced claim from the Deep Reviewer.

    ``sources`` is a list of tags — an evidence item can cite multiple
    sources, and a claim supported by multiple independent signals is
    stronger than one propped up by a single source. Each tag identifies a
    specific piece of the provided evidence (page text, a screenshot, a
    finding kind, or the verdict).
    """

    sources: list[str]
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"sources": list(self.sources), "text": self.text}


@dataclass(slots=True)
class SupportingPillar:
    """One of the 2-4 Minto-style pillars supporting the governing thought.

    Each pillar opens with a short ``claim`` (the top-line the pillar is
    making) and is backed by one or more pieces of ``evidence``. The claim
    answers "why?" with respect to the governing thought; the evidence
    answers "how do you know?" with pointers back into the provided data.
    """

    claim: str
    evidence: list[SourcedClaim] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass(slots=True)
class DeepReview:
    """Track 2 output: an evidence-grounded Minto-style review.

    The Minto Pyramid frame — governing thought at the top, 2-4 MECE pillars
    supporting it, then contradictions and caveats at the bottom — lets the
    reviewer act as "evidence in support of the verdict" rather than a second
    analyst. Validation still enforces: every pillar-evidence and every
    contradiction must cite only the provided source tags; the reviewer may
    not contradict the deterministic verdict's risk band.
    """

    governing_thought: str
    supporting_pillars: list[SupportingPillar] = field(default_factory=list)
    contradictions: list[SourcedClaim] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    # Schema version so the UI renderer (and any future migrations) can tell
    # old-shape reviews from new-shape ones. Old Track 2 reviews stored under
    # schema_version=1 had a different shape (summary/observations/concerns).
    schema_version: int = 2
    model: str = ""
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "governing_thought": self.governing_thought,
            "supporting_pillars": [p.to_dict() for p in self.supporting_pillars],
            "contradictions": [c.to_dict() for c in self.contradictions],
            "caveats": list(self.caveats),
            "schema_version": self.schema_version,
            "model": self.model,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Legacy v1 types — kept for type-checking old stored reviews only. Do not
# write new code against these; the Deep Reviewer emits the v2 shape above.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourcedObservation:
    source: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Narrative:
    headline: str                                           # one sentence, verdict
    why: str                                                # 2-3 sentences, reasoning
    recommendation: str                                     # one sentence, next step
    signal_explanations: list[SignalExplanation] = field(default_factory=list)
    # Metadata so we can debug / roll back bad narratives without guesswork.
    model: str = ""
    # One of: "llm" (LLM produced this and it validated),
    #         "skipped_no_key" (no ANTHROPIC_API_KEY — we didn't call the LLM),
    #         "skipped_error" (call failed or output rejected; caller used fallback).
    # The ``skipped_*`` modes mean this Narrative object won't exist; only "llm" gets stored.
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "why": self.why,
            "recommendation": self.recommendation,
            "signal_explanations": [s.to_dict() for s in self.signal_explanations],
            "model": self.model,
            "source": self.source,
        }
