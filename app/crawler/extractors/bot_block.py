"""Detect anti-bot interstitials (Cloudflare, reCAPTCHA, hCaptcha, Amazon Sorry, etc.).

Why this exists:
The crawler can fetch a page successfully (HTTP 200) and still be looking at a
challenge wall instead of the real site. Without flagging that, downstream
extractors will report "no signal" and a reviewer will read the absence as
"site looks clean" — when in reality we never saw the site at all.

We emit a single ``crawl.bot_block_detected`` evidence row with the matched
provider in ``details.provider`` and a short snippet for the reviewer. Severity
is ``medium`` because it's an investigative dead-end, not a risk signal in
itself — Amazon, for example, blocks crawlers routinely without being
suspicious. The pipeline can use this signal to mark the page as "unreadable"
so that absence-of-signal isn't treated as evidence of cleanliness.
"""
from __future__ import annotations

from app.crawler.extractors.base import ExtractContext, ExtractorResult, ProposedEvidence

# Tuples of (provider, lowercase substring/marker). Substrings are intentionally
# specific — we'd rather miss a block than falsely flag a real page.
_HTML_MARKERS: tuple[tuple[str, str], ...] = (
    ("cloudflare", "just a moment..."),
    ("cloudflare", "cf-browser-verification"),
    ("cloudflare", "cf-challenge-running"),
    ("cloudflare", "checking your browser before accessing"),
    ("cloudflare", "ray id:"),
    ("recaptcha", "g-recaptcha"),
    ("recaptcha", "www.google.com/recaptcha/api.js"),
    ("hcaptcha", "h-captcha"),
    ("hcaptcha", "hcaptcha.com/1/api.js"),
    ("amazon_sorry", "to discuss automated access to amazon data"),
    ("amazon_sorry", "/errors/validatecaptcha"),
    ("akamai", "reference&#32;&#35;"),     # Akamai "Access Denied" reference id
    ("akamai", "access denied"),
    ("perimeterx", "px-captcha"),
    ("perimeterx", "_pxhd"),
    ("datadome", "dd_cookie_test"),
    ("datadome", "datadome-captcha"),
    ("imperva", "incident id:"),
    ("imperva", "_incapsula_resource"),
)

# Title-only fast-path: if the page's <title> is one of these, we're confident
# enough to short-circuit the body scan.
_TITLE_MARKERS: tuple[tuple[str, str], ...] = (
    ("cloudflare", "just a moment"),
    ("cloudflare", "attention required! | cloudflare"),
    ("amazon_sorry", "sorry! something went wrong!"),
    ("akamai", "access denied"),
    ("imperva", "request unsuccessful"),
)


def extract_bot_block(ctx: ExtractContext) -> ExtractorResult:
    title_lower = (ctx.title or "").strip().lower()
    body_lower = (ctx.html or "").lower()

    matched: tuple[str, str] | None = None  # (provider, marker)

    # 1) Title fast-path
    for provider, marker in _TITLE_MARKERS:
        if marker in title_lower:
            matched = (provider, f"title: {marker}")
            break

    # 2) HTML body markers — only if title didn't already match
    if matched is None:
        for provider, marker in _HTML_MARKERS:
            if marker in body_lower:
                matched = (provider, marker)
                break

    # 3) Status-code corroboration. 403/429/503 with a tiny body is a strong
    # additional hint; we only use this as a confidence boost, not a primary
    # signal, because legitimate 4xx/5xx pages also exist.
    extracted = {"bot_block": False}
    if matched is None:
        return ExtractorResult(extracted=extracted)

    provider, marker = matched
    confidence = 0.85
    if ctx.status in (403, 429, 503):
        confidence = 0.95

    extracted["bot_block"] = True
    extracted["bot_block_provider"] = provider

    return ExtractorResult(
        extracted=extracted,
        evidence=[
            ProposedEvidence(
                kind="crawl.bot_block_detected",
                severity="medium",
                summary=f"Anti-bot interstitial detected ({provider}); page contents unreliable",
                confidence=confidence,
                details={
                    "provider": provider,
                    "matched_marker": marker[:120],
                    "status": ctx.status,
                    "title": (ctx.title or "")[:160],
                },
            )
        ],
    )
