"""Deterministic risk aggregator.

Given the evidence rows an investigation produced, this module decides:

* ``risk_band`` — one of ``critical`` / ``high`` / ``medium`` / ``low`` / ``none``
  / ``insufficient``.
* ``confidence`` — 0..1, how sure we are of that band.
* ``findings`` — the top signals that drove the band (machine-readable).
* ``summary`` — a short, reviewer-friendly sentence or two.

Design notes
------------
* **Deterministic and pure.** Takes an iterable of dicts (or an object with the
  right attributes) and returns a ``Verdict`` dataclass. No I/O.
* **Severity × confidence scoring.** Every evidence row contributes
  ``weight(severity) × confidence`` to a total. Bands are thresholds on the total.
  This gives corroboration naturally: two HIGHs beat one.
* **Visibility overrides.** If we literally couldn't see the site — because it
  was bot-blocked, or the seed URL failed to load, and nothing else came in —
  we return ``insufficient`` rather than ``none``. "No evidence" is not the
  same as "we saw nothing wrong."
* **No_signal rows are not negative evidence.** They mean "this analyzer ran
  and found nothing"; they contribute 0 to the score, and we don't call a site
  clean just because a few analyzers shrugged.

Adding a new band threshold or a new override is a tiny code change with a test.
Changing the *shape* of Verdict is a schema change — coordinate with the API and
report writer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol


# ---------------------------------------------------------------------------
# Scoring weights and thresholds
# ---------------------------------------------------------------------------

# Severity → base weight. These numbers are not magic; they're tuned so that:
#   * one CRITICAL finding at decent confidence lands in the CRITICAL band,
#   * two HIGHs corroborate into HIGH,
#   * a lone HIGH at 0.85 conf lands in MEDIUM (uncorroborated — suspicious
#     but not yet a verdict).
# Change with tests; the thresholds below move together.
_SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 5.0,
    "high": 2.0,
    "medium": 0.8,
    "low": 0.2,
    "info": 0.0,
}

# Band thresholds on cumulative score. Ordered high→low; first match wins.
_BAND_THRESHOLDS: list[tuple[str, float]] = [
    ("critical", 5.0),
    ("high", 3.0),
    ("medium", 1.2),
    ("low", 0.3),
]

# Kinds that indicate we couldn't see the site (as opposed to saw-it-and-it's-fine).
# If the investigation's only meaningful signals are these, we return
# ``insufficient`` — never "none". "We got blocked" ≠ "site is clean".
_VISIBILITY_BLOCKERS: frozenset[str] = frozenset({
    "crawl.bot_block_detected",
    "crawl.seed_unreachable",
})

# Kinds whose presence means "analyzer ran and found nothing interesting".
# These are bookkeeping, not risk signals. Never let them surface as a finding.
_NO_SIGNAL_SUFFIX = ".no_signal"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class _EvidenceLike(Protocol):
    """Structural type: anything with these attrs is acceptable input.

    Both SQLAlchemy ``Evidence`` rows and plain dicts (via ``_coerce``) work.
    """

    kind: str
    severity: str
    confidence: float
    summary: str


@dataclass(slots=True)
class Finding:
    """One of the top signals that drove the verdict."""

    kind: str
    severity: str
    confidence: float
    summary: str
    score: float  # weight(severity) * confidence — the ranking key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Verdict:
    """The aggregator's output. Serializable and stable across runs."""

    risk_band: str                          # critical|high|medium|low|none|insufficient
    confidence: float                       # 0..1
    summary: str                            # short human-readable narrative
    findings: list[Finding] = field(default_factory=list)
    score: float = 0.0                      # raw cumulative score, for debugging
    # Why this band was chosen — useful for tests and for the report writer:
    #   "threshold"          — score crossed a band boundary
    #   "visibility_blocked" — bot_block / seed_unreachable with nothing else
    #   "no_evidence"        — empty or only info/no_signal rows
    reason: str = "threshold"

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_band": self.risk_band,
            "confidence": self.confidence,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "score": self.score,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def aggregate(evidence: Iterable[_EvidenceLike | dict[str, Any]]) -> Verdict:
    """Aggregate a list of evidence rows into a Verdict.

    Accepts either SQLAlchemy ``Evidence`` ORM rows or plain dicts with the
    same field names (``kind``, ``severity``, ``confidence``, ``summary``).
    """
    rows = [_coerce(e) for e in evidence]

    # Filter out the "no_signal" bookkeeping kinds up front — they never score
    # and never appear as a finding.
    meaningful = [r for r in rows if not r["kind"].endswith(_NO_SIGNAL_SUFFIX)]

    # Also drop severity=info: in our vocabulary info rows are observations
    # ("we matched seed and homepage") rather than risk, and their weight is 0
    # anyway. Keep them out of findings so the user-visible list is concentrated.
    scored = [r for r in meaningful if r["severity"] != "info"]

    if not scored:
        return _verdict_none(rows)

    # --- score every row and rank ---
    findings: list[Finding] = []
    total = 0.0
    for r in scored:
        w = _SEVERITY_WEIGHT.get(r["severity"], 0.0)
        score = w * float(r["confidence"])
        if score <= 0:
            continue
        total += score
        findings.append(
            Finding(
                kind=r["kind"],
                severity=r["severity"],
                confidence=float(r["confidence"]),
                summary=str(r["summary"]),
                score=score,
            )
        )
    findings.sort(key=lambda f: f.score, reverse=True)

    # --- visibility override: bot_block / seed_unreachable with nothing else ---
    # "Nothing else" = no finding whose severity is at or above HIGH. A lone
    # visibility blocker alongside only low/medium signals still counts as
    # "we couldn't really see it" because the blocker itself is the dominant fact.
    high_or_above_present = any(
        f.severity in ("high", "critical") and f.kind not in _VISIBILITY_BLOCKERS
        for f in findings
    )
    visibility_blocker = next(
        (f for f in findings if f.kind in _VISIBILITY_BLOCKERS), None
    )
    if visibility_blocker is not None and not high_or_above_present:
        return _verdict_insufficient(visibility_blocker, findings, total)

    # --- threshold-based band ---
    band = _score_to_band(total)
    if band == "none":
        # Score was above 0 but below the LOW threshold — rare but possible if
        # we only collected faint signals. Surface as "low" with the cap
        # explained; never silently swallow risk.
        band = "low" if total > 0 else "none"

    confidence = _band_confidence(band, findings)
    summary = _template_summary(band, findings[:3])
    return Verdict(
        risk_band=band,
        confidence=confidence,
        summary=summary,
        findings=findings[:5],
        score=round(total, 3),
        reason="threshold",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _coerce(e: _EvidenceLike | dict[str, Any]) -> dict[str, Any]:
    """Normalize either an ORM row or a dict into the fields we care about."""
    if isinstance(e, dict):
        return {
            "kind": e["kind"],
            "severity": e["severity"],
            "confidence": float(e.get("confidence", 0.5)),
            "summary": e.get("summary", ""),
        }
    return {
        "kind": getattr(e, "kind"),
        "severity": getattr(e, "severity"),
        "confidence": float(getattr(e, "confidence", 0.5)),
        "summary": getattr(e, "summary", ""),
    }


def _score_to_band(total: float) -> str:
    for band, threshold in _BAND_THRESHOLDS:
        if total >= threshold:
            return band
    return "none"


def _band_confidence(band: str, findings: list[Finding]) -> float:
    """Derive overall confidence from the findings that drove the band.

    Rules of thumb:
    - critical/high: take the max confidence among the top 2 drivers.
    - medium/low:    take the mean confidence of the top 3 drivers.
    - none:          0.6 — we saw nothing, but we might have missed something.
    """
    if not findings:
        return 0.6 if band == "none" else 0.5
    if band in ("critical", "high"):
        top = findings[:2]
        return round(max(f.confidence for f in top), 3)
    if band in ("medium", "low"):
        top = findings[:3]
        return round(sum(f.confidence for f in top) / len(top), 3)
    return 0.6  # none


def _verdict_none(all_rows: list[dict[str, Any]]) -> Verdict:
    """Return the 'no meaningful evidence' verdict.

    Distinguish three cases, all of which resolve to band=none but deserve
    different prose so reviewers aren't misled:

    * empty rows → the pipeline never even ran (unexpected but handled).
    * only bookkeeping rows (crawl.plan, sitemap_found, etc. at INFO) → we did
      crawl the site and none of our analyzers had anything to say. This is
      the most common "clean site" path and must NOT be called "no evidence
      was collected".
    * at least one explicit *.no_signal row → we ran an analyzer that reported
      it actively saw nothing worth reporting; slightly stronger confidence.
    """
    if not all_rows:
        # Truly nothing — pipeline didn't emit even the always-on crawl.plan.
        return Verdict(
            risk_band="none",
            confidence=0.3,
            summary="No evidence was collected for this investigation.",
            findings=[],
            score=0.0,
            reason="no_evidence",
        )

    has_no_signal = any(r["kind"].endswith(_NO_SIGNAL_SUFFIX) for r in all_rows)
    summary = (
        "The crawl completed and our analyzers did not flag any risk signals "
        "on the pages examined."
    )
    return Verdict(
        risk_band="none",
        confidence=0.65 if has_no_signal else 0.6,
        summary=summary,
        findings=[],
        score=0.0,
        reason="no_evidence",
    )


def _verdict_insufficient(
    blocker: Finding, findings: list[Finding], total: float
) -> Verdict:
    """Return the 'we couldn't see the site' verdict.

    Distinct from 'none': band is explicitly ``insufficient`` so downstream
    consumers (UI, report writer) can show a different treatment — "try again
    later", "needs human review", etc.
    """
    if blocker.kind == "crawl.bot_block_detected":
        why = "the site blocked our crawler with an anti-bot challenge"
    else:  # seed_unreachable
        why = "the URL you submitted did not load"
    summary = f"Verdict deferred — {why}. We could not collect enough evidence to judge this site."
    return Verdict(
        risk_band="insufficient",
        confidence=0.3,
        summary=summary,
        findings=findings[:5],
        score=round(total, 3),
        reason="visibility_blocked",
    )


# ---------------------------------------------------------------------------
# Summary templates
# ---------------------------------------------------------------------------


def _template_summary(band: str, top: list[Finding]) -> str:
    """Build a short deterministic summary from band + top findings.

    Kept template-based on purpose: the LLM report writer (later milestone)
    will replace this with proper prose. For now this is what the API returns
    and what a reviewer sees first.
    """
    if not top:
        return "No risk signals surfaced."

    lead = top[0].summary.rstrip(".")
    extras = [f.summary.rstrip(".") for f in top[1:]]

    if band == "critical":
        opener = "Critical risk — this site shows strong scam indicators."
    elif band == "high":
        opener = "High risk — several signals suggest this site should not be trusted."
    elif band == "medium":
        opener = "Moderate risk — worth a human review before trusting this site."
    elif band == "low":
        opener = "Low risk — minor signals only, nothing conclusive."
    else:
        opener = "No meaningful risk detected."

    tail = f" The strongest signal: {lead}."
    if len(extras) == 1:
        tail += f" Also observed: {extras[0]}."
    elif len(extras) >= 2:
        tail += f" Also observed: {extras[0]}; and {extras[1]}."
    return opener + tail
