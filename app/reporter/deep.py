"""Deep Reviewer — Track 2 LLM report (Minto's Pyramid shape).

Unlike the translator (``writer.py``) which only sees the verdict, this track
gets the raw evidence: page text, screenshots, and the *entire* evidence
table — not just the top-ranked findings the aggregator surfaced. The LLM
produces a Minto-style review (governing thought → 2-4 pillars of claim +
evidence → contradictions → caveats) that complements the deterministic
verdict.

Why all evidence, not just top-5 findings?
------------------------------------------
As more analyzers get added (DNS, news sources, media) their outputs land in
the evidence table. Passing only ``verdict.findings[:8]`` would hide those
signals from the deep reviewer and force another refactor per analyzer. The
cost of including all evidence rows is small (the aggregator already bounds
it), and it lets the Deep Reviewer see the whole picture.

Call path: :func:`deep_review` is invoked from a dedicated API endpoint when
the user clicks "Generate Deep Review" in the UI. It's deliberately NOT wired
into the worker — deep reviews are expensive (vision + long context) and not
every investigation needs one. The endpoint caches the result in
``investigations.deep_review`` so repeat clicks are free.

Safety posture
--------------
* Every evidence item and every contradiction must cite only provided
  sources. Items with an unknown source are rejected.
* The governing thought must not contradict the verdict's risk band (we
  block outputs whose tone disagrees with a high/critical verdict).
* No invented URLs/domains (same foreign-domain tripwire as Track 1).
* No recommendations (that's Track 1's job — rejected if present).

If anything goes wrong, we return ``None`` and the UI shows a "deep review
unavailable" state. Never raises.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.aggregator import Verdict, aggregate
from app.crawler.urls import normalize_url
from app.models import Evidence, Investigation, Page
from app.reporter.deep_prompts import DEEP_REVIEW_SYSTEM_PROMPT
from app.reporter.narrative import (
    DeepReview,
    SourcedClaim,
    SupportingPillar,
)
from app.storage import get_storage

log = structlog.get_logger(__name__)


_DEFAULT_MODEL = os.getenv("DEEP_REVIEW_MODEL", "claude-sonnet-4-6")
_MAX_TOKENS = 1600
_TEMPERATURE = 0.2  # Slightly above 0 — deep review needs some flexibility to
                    # observe without being formulaic, but not creative enough
                    # to invent.

# Text budgets — keep the payload moderate so cost stays predictable.
_PAGE_TEXT_CAP = 3500           # chars from each page's readable_text
_EVIDENCE_DETAIL_CAP = 700      # chars per evidence.details (stringified)
_EVIDENCE_SUMMARY_CAP = 300     # chars per evidence.summary
_MAX_EVIDENCE_ROWS = 40         # upper bound on rows passed to the LLM
_MAX_SCREENSHOTS = 3            # seed + homepage + at most one extra page
_MAX_EXTRA_PAGES = 3            # text blocks beyond seed + homepage
_EXTRA_PAGE_TEXT_CAP = 2000     # tighter than _PAGE_TEXT_CAP for the extras

# Output caps — enforced after the LLM returns.
_CAP_GOVERNING = 240
_CAP_CLAIM = 200
_CAP_ITEM_TEXT = 300
_CAP_CAVEAT = 300

# Pillar / list bounds. Minto calls for 2-4 MECE pillars; below 2 is an
# unstructured paragraph, above 4 is a laundry list pretending to be a
# pyramid.
_MIN_PILLARS = 2
_MAX_PILLARS = 4
_MIN_EVIDENCE_PER_PILLAR = 1
_MAX_EVIDENCE_PER_PILLAR = 4
_MAX_CONTRADICTIONS = 6
_MAX_CAVEATS = 6
_MAX_SOURCES_PER_ITEM = 4

# Valid source tags independent of the specific investigation. Page-text and
# finding-kind sources are added dynamically per call (see ``deep_review``).
_VALID_SOURCES_STATIC: frozenset[str] = frozenset({
    "seed_page_text",
    "homepage_text",
    "screenshot:seed",
    "screenshot:homepage",
    "verdict",
})

_URLISH_RE = re.compile(
    r"\b(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s,]*)?",
    re.IGNORECASE,
)

# File extensions the LLM frequently quotes from page content (filenames, asset
# references). They look domain-shaped to a loose regex ("home.html" → host
# "home" TLD "html"), so we skip them in the foreign-domain tripwire.
_FILE_EXTENSIONS: frozenset[str] = frozenset({
    # markup / web
    "html", "htm", "xhtml", "xml", "rss", "atom",
    # scripts / source
    "js", "mjs", "cjs", "ts", "jsx", "tsx", "py", "rb", "php", "asp",
    "aspx", "jsp", "java", "go", "rs", "c", "cpp", "h", "hpp", "sh",
    # styles
    "css", "scss", "sass", "less",
    # images
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp", "avif", "bmp", "tiff",
    # docs
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf",
    # data
    "json", "yaml", "yml", "csv", "tsv", "sql", "log",
    # archives / binaries
    "zip", "tar", "gz", "bz2", "7z", "rar", "dmg", "exe", "bin",
    # media
    "mp3", "mp4", "wav", "avi", "mov", "mkv", "webm", "flac", "ogg",
    # fonts
    "woff", "woff2", "ttf", "otf", "eot",
})

# Phrases the LLM sometimes uses that contradict a high-risk verdict. Narrow
# list on purpose — false positives here reject otherwise-correct reviews.
_CONTRADICT_PATTERNS_FOR_HIGH_VERDICT = (
    "this site appears legitimate",
    "this site is legitimate",
    "this site appears safe",
    "this site is safe",
    "no concerns",
    "no risks",
    "no risk signals",
)

# Phrases that look like recommendations — forbidden in Track 2 (Track 1's
# job). If we see them we reject; the prompt already forbids them.
_RECOMMENDATION_PATTERNS = (
    "you should",
    "we recommend",
    "users should",
    "we advise",
    "avoid entering",
    "do not enter",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def deep_review(
    investigation_id: uuid.UUID,
    session,
    *,
    api_key: str | None = None,
) -> DeepReview | None:
    """Produce a Minto-style deep review for an investigation, or return None.

    Parameters
    ----------
    investigation_id
        The investigation to review.
    session
        An open :class:`AsyncSession`. The caller owns commit/close.
    api_key
        Optional user-supplied Anthropic API key. When ``None``, falls back to
        the ``ANTHROPIC_API_KEY`` env var. This is the BYOK path — the key is
        used for exactly this call and not persisted anywhere.

    Returns
    -------
    DeepReview on success; None if no API key is available, the investigation
    isn't found, there's no evidence to review, the LLM call fails, or the
    output fails validation.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.info("deep_review.skip", reason="no_api_key")
        return None

    try:
        # Late import so the module loads even if anthropic isn't installed
        # (tests run without the dep).
        from anthropic import AsyncAnthropic
    except ImportError:
        log.warning("deep_review.skip", reason="anthropic_not_installed")
        return None

    # Load everything in one go.
    inv = (
        await session.execute(
            select(Investigation)
            .where(Investigation.id == investigation_id)
            .options(
                selectinload(Investigation.pages),
                selectinload(Investigation.evidence),
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        log.warning("deep_review.missing_inv", id=str(investigation_id))
        return None

    # Re-derive the verdict from the evidence we actually have on disk. This
    # keeps the deep reviewer honest even if the stored verdict was out of
    # date (e.g., aggregator rules changed since the investigation ran).
    verdict = aggregate(inv.evidence)

    seed_page = _pick_page(inv.pages, "is_seed")
    homepage = _pick_page(inv.pages, "is_homepage_compare")
    extra_pages = _pick_extra_pages(
        inv.pages,
        inv_origin=inv.normalized_origin,
        exclude={p.id for p in (seed_page, homepage) if p is not None},
        limit=_MAX_EXTRA_PAGES,
    )

    # If we have absolutely no pages and no evidence rows, the deep reviewer
    # has nothing to work with — bail and let the UI show Track 1 alone.
    if seed_page is None and homepage is None and not extra_pages and not inv.evidence:
        log.info("deep_review.skip", reason="no_pages_no_evidence", id=str(investigation_id))
        return None

    # Fetch screenshots in a threadpool — boto3 is blocking.
    # Order: seed, homepage, then at most one "most interesting" extra. The
    # extra screenshot is prioritized for off-origin pages.
    screenshots: list[tuple[str, bytes]] = []
    screenshot_candidates: list[tuple[str, Page]] = []
    if seed_page is not None:
        screenshot_candidates.append(("seed", seed_page))
    if homepage is not None:
        screenshot_candidates.append(("homepage", homepage))
    for i, extra in enumerate(extra_pages):
        if _is_off_origin(extra, inv.normalized_origin):
            screenshot_candidates.append((f"extra:{i}", extra))
            break  # only one extra screenshot
    for label, page in screenshot_candidates:
        if not page.ato_screenshot_key:
            continue
        data = await asyncio.to_thread(get_storage().get_bytes, page.ato_screenshot_key)
        if data:
            screenshots.append((label, data))
        if len(screenshots) >= _MAX_SCREENSHOTS:
            break

    # Build the set of source tags we'll accept in the output. We ONLY include
    # a source tag if we actually provided the corresponding input — citing
    # something we didn't pass is a hallucination.
    allowed_sources: set[str] = set(_VALID_SOURCES_STATIC)
    if seed_page is None:
        allowed_sources.discard("seed_page_text")
        allowed_sources.discard("screenshot:seed")
    if homepage is None:
        allowed_sources.discard("homepage_text")
        allowed_sources.discard("screenshot:homepage")
    if not any(label == "seed" for label, _ in screenshots):
        allowed_sources.discard("screenshot:seed")
    if not any(label == "homepage" for label, _ in screenshots):
        allowed_sources.discard("screenshot:homepage")
    for i, _extra in enumerate(extra_pages):
        allowed_sources.add(f"extra_page_text:{i}")
    for label, _ in screenshots:
        if label.startswith("extra:"):
            allowed_sources.add(f"screenshot:{label}")
    # All evidence kinds are valid sources — not just the top-ranked findings.
    for row in inv.evidence[:_MAX_EVIDENCE_ROWS]:
        if row.kind:
            allowed_sources.add(f"finding:{row.kind}")

    payload_text = _build_text_payload(
        url=inv.input_url,
        verdict=verdict,
        evidence_rows=inv.evidence,
        seed_page=seed_page,
        homepage=homepage,
        extra_pages=extra_pages,
        inv_origin=inv.normalized_origin,
        screenshots_provided=[label for label, _ in screenshots],
    )

    # Multimodal user message. Text block first, then screenshot label +
    # image for each screenshot provided.
    content_blocks: list[dict[str, Any]] = [{"type": "text", "text": payload_text}]
    for label, data in screenshots:
        content_blocks.append(
            {"type": "text", "text": f"\n[screenshot:{label}]"}
        )
        content_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )

    client = AsyncAnthropic(api_key=api_key)
    try:
        # No assistant-prefill turn: claude-sonnet-4.x rejects assistant
        # messages after multimodal input. The system prompt instructs JSON-
        # only output, and ``_isolate_json`` below is tolerant to occasional
        # fence wrapping.
        resp = await client.messages.create(
            model=_DEFAULT_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=DEEP_REVIEW_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": content_blocks},
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("deep_review.api_error", err=str(e))
        return None

    raw = _extract_text(resp)
    body = _isolate_json(raw)
    if body is None:
        log.warning("deep_review.reject", reason="no_json_object_in_output")
        return None

    # URLs we put in the payload are fair game for the LLM to mention. Collect
    # them so the foreign-domain tripwire doesn't flag e.g. the off-origin
    # page we deliberately surfaced.
    extra_urls: list[str] = []
    for p in extra_pages:
        if p.url:
            extra_urls.append(p.url)
        if p.final_url:
            extra_urls.append(p.final_url)

    result = _parse_and_validate(
        body,
        verdict=verdict,
        url=inv.input_url,
        allowed_sources=allowed_sources,
        extra_allowed_urls=extra_urls,
    )
    if result is None:
        return None
    result.model = _DEFAULT_MODEL
    result.source = "llm"
    log.info(
        "deep_review.ok",
        id=str(investigation_id),
        model=_DEFAULT_MODEL,
        pillars=len(result.supporting_pillars),
        contradictions=len(result.contradictions),
        caveats=len(result.caveats),
    )
    return result


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def _pick_page(pages: list[Page], flag: str) -> Page | None:
    """Find the seed page (or homepage compare page) from the page list."""
    for p in pages:
        if (p.extracted or {}).get(flag):
            return p
    return None


def _origin_of(url: str | None) -> str | None:
    """Return scheme://host[:port] for a URL, or None if unparseable."""
    if not url:
        return None
    try:
        return normalize_url(url).origin
    except Exception:  # noqa: BLE001
        return None


def _is_off_origin(page: Page, inv_origin: str) -> bool:
    """True if this page's final URL belongs to a different origin than the site.

    We compare against ``final_url`` (post-redirect) so a link that hops off the
    registered domain is caught even when the href was same-origin.
    """
    target = page.final_url or page.url
    other = _origin_of(target)
    return bool(other and other != inv_origin)


def _pick_extra_pages(
    pages: list[Page],
    *,
    inv_origin: str,
    exclude: set[uuid.UUID],
    limit: int,
) -> list[Page]:
    """Select the most review-worthy non-seed/non-homepage pages.

    Ranking priorities, highest first:
      1. Off-origin pages (the kohphanganrooms → floatingbluedock case).
      2. Non-200 pages on nav-worthy paths (4xx on /about, /contact, ...).
      3. Long readable pages the deterministic layer never got to see.

    Pages with zero meaningful score are dropped — they only burn tokens.
    """
    scored: list[tuple[float, Page]] = []
    for p in pages:
        if p.id in exclude:
            continue
        score = 0.0
        if _is_off_origin(p, inv_origin):
            score += 100.0
        status = p.http_status or 0
        if status and status >= 400:
            score += 40.0
        wc = p.word_count or 0
        score += min(wc / 30.0, 40.0)
        if score <= 0:
            continue
        scored.append((score, p))
    # Sort by score desc; break ties with fetched_at asc for determinism.
    scored.sort(key=lambda t: (-t[0], t[1].fetched_at))
    return [p for _, p in scored[:limit]]


def _build_text_payload(
    *,
    url: str,
    verdict: Verdict,
    evidence_rows: list[Evidence],
    seed_page: Page | None,
    homepage: Page | None,
    extra_pages: list[Page],
    inv_origin: str,
    screenshots_provided: list[str],
) -> str:
    """Assemble the text half of the multimodal input.

    We dump it as a structured JSON block so the LLM treats the payload as
    the complete universe of facts. Unlike the old version, EVIDENCE is the
    full evidence table (bounded by ``_MAX_EVIDENCE_ROWS``), not just the
    top-ranked findings — future analyzers (DNS, media, news sources) land
    here automatically.
    """
    evidence_payload: list[dict[str, Any]] = []
    for row in evidence_rows[:_MAX_EVIDENCE_ROWS]:
        summary_text = (row.summary or "")[:_EVIDENCE_SUMMARY_CAP]
        if row.details:
            details_str = json.dumps(row.details, ensure_ascii=False, sort_keys=True)
            if len(details_str) > _EVIDENCE_DETAIL_CAP:
                details_str = details_str[:_EVIDENCE_DETAIL_CAP] + "... [truncated]"
        else:
            details_str = ""
        evidence_payload.append({
            "kind": row.kind,
            "severity": row.severity,
            "confidence": round(float(row.confidence or 0.0), 3),
            "summary": summary_text,
            "details": details_str,
        })

    seed_payload = _page_payload(seed_page, _PAGE_TEXT_CAP) if seed_page else None
    home_payload = _page_payload(homepage, _PAGE_TEXT_CAP) if homepage else None
    extras_payload: list[dict[str, Any]] = []
    for i, p in enumerate(extra_pages):
        extras_payload.append({
            "source_tag": f"extra_page_text:{i}",
            "is_off_origin": _is_off_origin(p, inv_origin),
            **_page_payload(p, _EXTRA_PAGE_TEXT_CAP),
        })

    payload = {
        "URL": url,
        "SITE_ORIGIN": inv_origin,
        "VERDICT": {
            "risk_band": verdict.risk_band,
            "confidence": round(float(verdict.confidence), 3),
            "reason": verdict.reason,
        },
        "EVIDENCE": evidence_payload,
        "SEED_PAGE": seed_payload,
        "HOMEPAGE": home_payload,
        "EXTRA_PAGES": extras_payload,
        "SCREENSHOTS_PROVIDED": screenshots_provided,
    }
    return (
        "Here is the evidence. Treat it as the only universe of facts you may "
        "reference.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _page_payload(page: Page, text_cap: int) -> dict[str, Any]:
    ex = page.extracted or {}
    readable = str(ex.get("readable_text") or "")
    truncated = readable[:text_cap]
    if len(readable) > text_cap:
        truncated += "... [truncated]"
    return {
        "url": page.url,
        "final_url": page.final_url,
        "http_status": page.http_status,
        "title": page.title,
        "lang": page.lang,
        "word_count": page.word_count,
        "readable_text": truncated,
    }


def _extract_text(resp: Any) -> str:
    try:
        blocks = getattr(resp, "content", []) or []
        parts = [getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"]
        return "".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


def _isolate_json(raw: str) -> str | None:
    """Extract the JSON object from a model response.

    The system prompt instructs the model to emit *only* a JSON object with no
    fences or preamble, but we don't blindly trust that — LLMs sometimes wrap
    output in ```json ... ``` fences or add a one-line preamble. This helper
    locates the outermost ``{...}`` block using brace counting, respecting
    strings (so a ``}`` inside a quoted value doesn't prematurely close).

    Returns the trimmed JSON substring, or ``None`` if no balanced object is
    found.
    """
    if not raw:
        return None
    # Fast path: already a pure JSON object
    s = raw.strip()
    if s.startswith("{") and s.endswith("}"):
        return s

    # Fenced block: ```json ... ``` or ``` ... ```
    if "```" in s:
        first_fence = s.find("```")
        after = s[first_fence + 3:]
        nl = after.find("\n")
        if nl != -1:
            after = after[nl + 1:]
        end_fence = after.rfind("```")
        if end_fence != -1:
            s = after[:end_fence].strip()

    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _parse_and_validate(
    body: str,
    *,
    verdict: Verdict,
    url: str,
    allowed_sources: set[str],
    extra_allowed_urls: list[str] | None = None,
) -> DeepReview | None:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("deep_review.reject", reason="invalid_json", err=str(e))
        return None
    if not isinstance(obj, dict):
        log.warning("deep_review.reject", reason="not_object")
        return None

    # --- governing_thought ----------------------------------------------
    governing = obj.get("governing_thought")
    if not isinstance(governing, str) or not governing.strip() or len(governing) > _CAP_GOVERNING:
        log.warning("deep_review.reject", reason="bad_governing_thought")
        return None
    governing = governing.strip()

    # --- supporting_pillars ---------------------------------------------
    raw_pillars = obj.get("supporting_pillars")
    if not isinstance(raw_pillars, list):
        log.warning("deep_review.reject", reason="pillars_not_list")
        return None
    if len(raw_pillars) < _MIN_PILLARS or len(raw_pillars) > _MAX_PILLARS:
        log.warning(
            "deep_review.reject",
            reason="pillars_out_of_range",
            count=len(raw_pillars),
            min=_MIN_PILLARS,
            max=_MAX_PILLARS,
        )
        return None

    pillars: list[SupportingPillar] = []
    for idx, raw in enumerate(raw_pillars):
        if not isinstance(raw, dict):
            log.warning("deep_review.reject", reason="pillar_not_object", idx=idx)
            return None
        claim = raw.get("claim")
        evidence_raw = raw.get("evidence")
        if not isinstance(claim, str) or not claim.strip() or len(claim) > _CAP_CLAIM:
            log.warning("deep_review.reject", reason="pillar_bad_claim", idx=idx)
            return None
        if not isinstance(evidence_raw, list):
            log.warning("deep_review.reject", reason="pillar_evidence_not_list", idx=idx)
            return None
        if (
            len(evidence_raw) < _MIN_EVIDENCE_PER_PILLAR
            or len(evidence_raw) > _MAX_EVIDENCE_PER_PILLAR
        ):
            log.warning(
                "deep_review.reject",
                reason="pillar_evidence_out_of_range",
                idx=idx,
                count=len(evidence_raw),
            )
            return None

        evidence_items: list[SourcedClaim] = []
        for j, item in enumerate(evidence_raw):
            parsed = _parse_sourced_claim(item, allowed_sources, f"pillar[{idx}].evidence[{j}]")
            if parsed is None:
                return None
            evidence_items.append(parsed)

        pillars.append(SupportingPillar(claim=claim.strip(), evidence=evidence_items))

    # --- contradictions -------------------------------------------------
    contradictions_raw = obj.get("contradictions", [])
    if not isinstance(contradictions_raw, list):
        log.warning("deep_review.reject", reason="contradictions_not_list")
        return None
    if len(contradictions_raw) > _MAX_CONTRADICTIONS:
        log.warning(
            "deep_review.reject",
            reason="contradictions_too_many",
            count=len(contradictions_raw),
        )
        return None
    contradictions: list[SourcedClaim] = []
    for j, item in enumerate(contradictions_raw):
        parsed = _parse_sourced_claim(item, allowed_sources, f"contradictions[{j}]")
        if parsed is None:
            return None
        contradictions.append(parsed)

    # --- caveats --------------------------------------------------------
    caveats_raw = obj.get("caveats", [])
    if not isinstance(caveats_raw, list) or len(caveats_raw) > _MAX_CAVEATS:
        log.warning("deep_review.reject", reason="caveats_shape")
        return None
    caveats: list[str] = []
    for c in caveats_raw:
        if not isinstance(c, str) or len(c) > _CAP_CAVEAT:
            log.warning("deep_review.reject", reason="caveats_item_bad")
            return None
        if c.strip():
            caveats.append(c.strip())

    # --- foreign-domain check ------------------------------------------
    allowed_domains = _collect_allowed_domains(
        verdict, url, extra_urls=extra_allowed_urls or []
    )
    for field_label, text in _iter_text_fields(governing, pillars, contradictions, caveats):
        foreign = _find_foreign_domains(text, allowed_domains)
        if foreign:
            log.warning(
                "deep_review.reject",
                reason="foreign_domain",
                field=field_label,
                foreign=list(foreign),
            )
            return None

    # --- verdict-contradiction check -----------------------------------
    # Only enforce when the deterministic verdict is clearly bad
    # (critical/high). For low/medium/insufficient, nuance is allowed.
    if verdict.risk_band in ("critical", "high"):
        haystack_parts = [governing]
        for c in contradictions:
            haystack_parts.append(c.text)
        haystack = " ".join(haystack_parts).lower()
        for pat in _CONTRADICT_PATTERNS_FOR_HIGH_VERDICT:
            if pat in haystack:
                log.warning("deep_review.reject", reason="contradicts_verdict", pattern=pat)
                return None

    # --- no-recommendations check --------------------------------------
    # The prompt forbids recommendations; enforce so Track 2 stays in its
    # lane. We scan every text field.
    for field_label, text in _iter_text_fields(governing, pillars, contradictions, caveats):
        low = text.lower()
        for pat in _RECOMMENDATION_PATTERNS:
            if pat in low:
                log.warning(
                    "deep_review.reject",
                    reason="contains_recommendation",
                    field=field_label,
                    pattern=pat,
                )
                return None

    return DeepReview(
        governing_thought=governing,
        supporting_pillars=pillars,
        contradictions=contradictions,
        caveats=caveats,
        schema_version=2,
    )


def _parse_sourced_claim(
    item: Any,
    allowed_sources: set[str],
    field_label: str,
) -> SourcedClaim | None:
    """Validate one evidence/contradiction entry and return a SourcedClaim."""
    if not isinstance(item, dict):
        log.warning("deep_review.reject", reason="item_not_object", field=field_label)
        return None

    # Accept either "sources" (new shape) or a single "source" (defensive —
    # sometimes LLMs produce the singular form even when told not to).
    raw_sources = item.get("sources")
    if raw_sources is None and "source" in item:
        raw_sources = [item["source"]]
    if not isinstance(raw_sources, list) or not raw_sources:
        log.warning("deep_review.reject", reason="item_no_sources", field=field_label)
        return None
    if len(raw_sources) > _MAX_SOURCES_PER_ITEM:
        log.warning(
            "deep_review.reject",
            reason="item_too_many_sources",
            field=field_label,
            count=len(raw_sources),
        )
        return None

    sources: list[str] = []
    for src in raw_sources:
        if not isinstance(src, str):
            log.warning("deep_review.reject", reason="item_source_not_string", field=field_label)
            return None
        if src not in allowed_sources:
            log.warning(
                "deep_review.reject",
                reason="item_bad_source",
                field=field_label,
                source=src,
                allowed=sorted(allowed_sources),
            )
            return None
        sources.append(src)

    text = item.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > _CAP_ITEM_TEXT:
        log.warning("deep_review.reject", reason="item_bad_text", field=field_label)
        return None

    return SourcedClaim(sources=sources, text=text.strip())


def _iter_text_fields(
    governing: str,
    pillars: list[SupportingPillar],
    contradictions: list[SourcedClaim],
    caveats: list[str],
):
    """Yield every user-visible text field with a label for log context."""
    yield ("governing_thought", governing)
    for i, p in enumerate(pillars):
        yield (f"pillar[{i}].claim", p.claim)
        for j, e in enumerate(p.evidence):
            yield (f"pillar[{i}].evidence[{j}]", e.text)
    for j, c in enumerate(contradictions):
        yield (f"contradictions[{j}]", c.text)
    for j, c in enumerate(caveats):
        yield (f"caveats[{j}]", c)


def _collect_allowed_domains(
    verdict: Verdict,
    url: str,
    *,
    extra_urls: list[str] | None = None,
) -> set[str]:
    allowed: set[str] = set()
    for token in _URLISH_RE.findall(url):
        allowed.add(_normalize_domain(token))
    for f in verdict.findings:
        for token in _URLISH_RE.findall(f.summary or ""):
            allowed.add(_normalize_domain(token))
    for extra in extra_urls or ():
        if not extra:
            continue
        for token in _URLISH_RE.findall(extra):
            allowed.add(_normalize_domain(token))
    return {d for d in allowed if d}


def _normalize_domain(token: str) -> str:
    t = token.lower().strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0].rstrip(".,;:")
    return t


def _find_foreign_domains(text: str, allowed: set[str]) -> set[str]:
    foreign: set[str] = set()
    for token in _URLISH_RE.findall(text):
        dom = _normalize_domain(token)
        if not dom or dom in allowed:
            continue
        # Skip obvious filenames — "home.html", "styles.css" etc. The regex
        # treats the extension as a TLD, but these aren't hostnames. We only
        # skip when the token has a *single* dot (no subdomain) AND the
        # trailing segment is a known file extension.
        if dom.count(".") == 1:
            ext = dom.rsplit(".", 1)[1]
            if ext in _FILE_EXTENSIONS:
                continue
        if any(dom.endswith("." + a) or a.endswith("." + dom) for a in allowed):
            continue
        foreign.add(dom)
    return foreign
