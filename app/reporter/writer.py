"""LLM report writer.

Public entrypoint: :func:`write_report`. Given a verdict + url, either returns
a validated :class:`Narrative` or returns ``None`` — never raises. Callers
fall back to the deterministic aggregator summary on ``None``.

Validation layers
-----------------
The LLM can still go off-rails even with a tight system prompt. We defend with:

1. **Structural parse** — the response must be a JSON object matching the
   expected schema. No prose, no markdown fences, no trailing garbage.
2. **Kind-set check** — every ``kind`` in ``signal_explanations`` must exactly
   match one of the kinds we provided. Extra or missing entries = reject.
3. **Length caps** — hard character limits on each field to stop the model
   from producing a wall of speculation.
4. **Foreign-domain check** — the narrative body must not contain any URL or
   domain string that wasn't in the input. Any apparent domain that isn't the
   investigated host → reject.
5. **Verdict-preservation check** — if the LLM somehow restates a risk_band
   in its output, it must match the one we gave.

Any rejection here means we log and return ``None``. We never half-accept.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import structlog

from app.aggregator import Verdict
from app.reporter.deep import _isolate_json
from app.reporter.narrative import Narrative, SignalExplanation
from app.reporter.prompts import SYSTEM_PROMPT

log = structlog.get_logger(__name__)

# Default model: Claude Sonnet 4.6 — strong narrative quality without overkill
# for a short structured-output task. Override via REPORTER_MODEL if you want
# to try Haiku 4.5 for speed/cost. Prompt is unchanged across models.
_DEFAULT_MODEL = os.getenv("REPORTER_MODEL", "claude-sonnet-4-6")
_MAX_TOKENS = 700
_TEMPERATURE = 0.0  # We want stable, minimum-creativity rewrites.

# Field length caps — enforced post-hoc even though the prompt also states them.
# Belt-and-suspenders: LLMs treat "max 120 chars" as a hint, not a hard limit.
_CAP_HEADLINE = 160
_CAP_WHY = 600
_CAP_RECOMMENDATION = 280
_CAP_EXPLANATION = 280
_MAX_SIGNAL_EXPLANATIONS = 5

# URL-ish token detector — catches "example.com", "https://foo", "bar.co.uk", etc.
# Used for the foreign-domain check: any hit that doesn't appear in the input
# payload is treated as a hallucinated reference.
_URLISH_RE = re.compile(
    r"\b(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s,]*)?",
    re.IGNORECASE,
)

# File extensions — skipped by the foreign-domain check because tokens like
# "home.html" or "styles.css" are filenames, not hostnames, and the regex
# above can't tell the difference. Kept in sync with deep.py.
_FILE_EXTENSIONS: frozenset[str] = frozenset({
    "html", "htm", "xhtml", "xml", "rss", "atom",
    "js", "mjs", "cjs", "ts", "jsx", "tsx", "py", "rb", "php", "asp",
    "aspx", "jsp", "java", "go", "rs", "c", "cpp", "h", "hpp", "sh",
    "css", "scss", "sass", "less",
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp", "avif", "bmp", "tiff",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf",
    "json", "yaml", "yml", "csv", "tsv", "sql", "log",
    "zip", "tar", "gz", "bz2", "7z", "rar", "dmg", "exe", "bin",
    "mp3", "mp4", "wav", "avi", "mov", "mkv", "webm", "flac", "ogg",
    "woff", "woff2", "ttf", "otf", "eot",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_report(
    verdict: Verdict,
    url: str,
    *,
    api_key: str | None = None,
) -> Narrative | None:
    """Produce a plain-English narrative from a Verdict, or return None.

    Returns ``None`` when:
    - neither ``api_key`` nor the ``ANTHROPIC_API_KEY`` env var is set,
    - the anthropic package isn't installed,
    - the API call raises,
    - the LLM output fails any validation layer.

    Never raises. Caller should use the aggregator's template summary in
    those cases.

    ``api_key`` takes precedence over the env var when both are present. This
    lets callers (e.g., the worker) forward a user-supplied key per request
    without mutating process env.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.info("reporter.skip", reason="no_api_key")
        return None

    try:
        from anthropic import Anthropic  # lazy import; dep is optional
    except ImportError:
        log.warning("reporter.skip", reason="anthropic_not_installed")
        return None

    client = Anthropic(api_key=api_key)
    payload = _build_user_payload(verdict, url)

    try:
        # NOTE: no assistant-prefill turn. Earlier versions of this code ended
        # the conversation with ``{"role": "assistant", "content": "{"}`` to
        # force the model to commit to JSON, but claude-sonnet-4.x rejects
        # that shape with "This model does not support assistant message
        # prefill. The conversation must end with a user message." The
        # SYSTEM_PROMPT already pins JSON-only output, and ``_isolate_json``
        # below tolerates occasional fences or preambles.
        resp = client.messages.create(
            model=_DEFAULT_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
    except Exception as e:  # noqa: BLE001 — any SDK/network error is a fallback case
        log.warning("reporter.api_error", err=str(e))
        return None

    raw = _extract_text(resp)
    body = _isolate_json(raw)
    if body is None:
        log.warning("reporter.reject", reason="no_json_object_in_response")
        return None

    narrative = _parse_and_validate(body, verdict, url)
    if narrative is None:
        return None
    narrative.model = _DEFAULT_MODEL
    narrative.source = "llm"
    log.info(
        "reporter.ok",
        model=_DEFAULT_MODEL,
        risk_band=verdict.risk_band,
        signal_count=len(narrative.signal_explanations),
    )
    return narrative


# ---------------------------------------------------------------------------
# Internals — payload shape
# ---------------------------------------------------------------------------


def _build_user_payload(verdict: Verdict, url: str) -> dict[str, Any]:
    """Build the JSON payload sent as the user message.

    Deliberately *minimal*: only what the LLM needs to narrate. Every field we
    include is a field the LLM is allowed to reference — and nothing else. No
    page HTML, no evidence ``details`` blobs, no raw screenshots, no URLs beyond
    the investigation target.
    """
    return {
        "url": url,
        "verdict": {
            "risk_band": verdict.risk_band,
            "confidence": round(float(verdict.confidence), 3),
            "reason": verdict.reason,
        },
        "findings": [
            {
                "kind": f.kind,
                "severity": f.severity,
                "confidence": round(float(f.confidence), 3),
                "summary": f.summary,
            }
            for f in verdict.findings
        ],
    }


def _extract_text(resp: Any) -> str:
    """Pull the text out of an Anthropic Messages API response."""
    try:
        blocks = getattr(resp, "content", []) or []
        parts = [getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"]
        return "".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Internals — validation
# ---------------------------------------------------------------------------


def _parse_and_validate(body: str, verdict: Verdict, url: str) -> Narrative | None:
    """Run every validation layer; return Narrative or None."""
    # 1) Structural parse — JSON object
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("reporter.reject", reason="invalid_json", err=str(e))
        return None
    if not isinstance(obj, dict):
        log.warning("reporter.reject", reason="not_object")
        return None

    # 2) Required keys + types + length caps
    headline = _str_field(obj, "headline", _CAP_HEADLINE)
    why = _str_field(obj, "why", _CAP_WHY)
    recommendation = _str_field(obj, "recommendation", _CAP_RECOMMENDATION)
    if headline is None or why is None or recommendation is None:
        return None

    explanations_raw = obj.get("signal_explanations")
    if not isinstance(explanations_raw, list):
        log.warning("reporter.reject", reason="signal_explanations_not_list")
        return None
    if len(explanations_raw) > _MAX_SIGNAL_EXPLANATIONS:
        log.warning(
            "reporter.reject",
            reason="too_many_explanations",
            count=len(explanations_raw),
        )
        return None

    # 3) Kind-set check: the LLM can ONLY name kinds we gave it, and the
    #    count/order must match the findings we provided. This catches
    #    confabulation where the model invents a new finding category.
    provided_kinds = [f.kind for f in verdict.findings]
    if [e.get("kind") for e in explanations_raw] != provided_kinds:
        log.warning(
            "reporter.reject",
            reason="explanation_kinds_mismatch",
            provided=provided_kinds,
            got=[e.get("kind") for e in explanations_raw],
        )
        return None

    explanations: list[SignalExplanation] = []
    for item in explanations_raw:
        if not isinstance(item, dict):
            log.warning("reporter.reject", reason="explanation_not_object")
            return None
        kind = item.get("kind")
        plain = item.get("plain_english")
        if not isinstance(kind, str) or not isinstance(plain, str):
            log.warning("reporter.reject", reason="explanation_wrong_types")
            return None
        if len(plain) > _CAP_EXPLANATION:
            log.warning("reporter.reject", reason="explanation_too_long", kind=kind)
            return None
        explanations.append(SignalExplanation(kind=kind, plain_english=plain.strip()))

    # 4) Foreign-domain check: the narrative fields must not mention any
    #    domain/URL that wasn't in the input payload. This is the strongest
    #    practical hallucination tripwire — the classic failure mode is the
    #    LLM making up a plausible company or reference URL.
    allowed_domains = _allowed_domains(verdict, url)
    for field_name, value in (
        ("headline", headline),
        ("why", why),
        ("recommendation", recommendation),
        *[(f"explanation[{i}]", e.plain_english) for i, e in enumerate(explanations)],
    ):
        foreign = _find_foreign_domains(value, allowed_domains)
        if foreign:
            log.warning(
                "reporter.reject",
                reason="foreign_domain_mentioned",
                field=field_name,
                foreign=list(foreign),
            )
            return None

    # 5) Verdict-preservation check: if the LLM echoed a risk_band in the
    #    headline text, it must match what we gave. (Weak check — "critical"
    #    and "high" are substrings of normal words — but useful.)
    lowered_head = headline.lower()
    echoed_bands = [
        b for b in ("critical", "high", "medium", "low", "insufficient")
        if b in lowered_head
    ]
    if echoed_bands and verdict.risk_band not in echoed_bands and verdict.risk_band != "none":
        # Exception: "none" is awkward natural English; the LLM often writes
        # "no risk" / "no signals" instead, so we don't enforce it.
        log.warning(
            "reporter.reject",
            reason="verdict_mismatch",
            expected=verdict.risk_band,
            echoed=echoed_bands,
        )
        return None

    return Narrative(
        headline=headline.strip(),
        why=why.strip(),
        recommendation=recommendation.strip(),
        signal_explanations=explanations,
    )


def _str_field(obj: dict[str, Any], key: str, cap: int) -> str | None:
    """Require a non-empty string of length <= cap."""
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        log.warning("reporter.reject", reason="missing_or_nonstring_field", field=key)
        return None
    if len(v) > cap:
        log.warning("reporter.reject", reason="field_too_long", field=key, length=len(v))
        return None
    return v


def _allowed_domains(verdict: Verdict, url: str) -> set[str]:
    """Collect every domain token we provided to the LLM.

    These are the only domain strings allowed to appear in its output. Anything
    else is presumed hallucinated.
    """
    allowed: set[str] = set()
    # Domains from the input URL
    for token in _URLISH_RE.findall(url):
        allowed.add(_normalize_domain(token))
    # Domains mentioned in finding summaries
    for f in verdict.findings:
        for token in _URLISH_RE.findall(f.summary or ""):
            allowed.add(_normalize_domain(token))
    return {d for d in allowed if d}


def _normalize_domain(token: str) -> str:
    """Strip scheme/path, lowercase, trim. '/' etc."""
    t = token.lower().strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0].rstrip(".,;:")
    return t


def _find_foreign_domains(text: str, allowed: set[str]) -> set[str]:
    """Return any URL-ish token in text whose normalized domain is unknown."""
    foreign: set[str] = set()
    for token in _URLISH_RE.findall(text):
        dom = _normalize_domain(token)
        if not dom:
            continue
        # Filename guard — "home.html" / "styles.css" look domain-shaped but
        # aren't. Skip single-dot tokens whose trailing segment is a known
        # file extension.
        if dom.count(".") == 1:
            ext = dom.rsplit(".", 1)[1]
            if ext in _FILE_EXTENSIONS:
                continue
        # Allow exact match or suffix match (so "www.example.com" is OK if
        # "example.com" was provided, and vice versa).
        if dom in allowed:
            continue
        if any(dom.endswith("." + a) or a.endswith("." + dom) for a in allowed):
            continue
        foreign.add(dom)
    return foreign
