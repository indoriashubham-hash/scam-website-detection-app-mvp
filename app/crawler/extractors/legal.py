"""Legal-page classifier.

We detect when the current URL is a legal page (tos/privacy/refund/shipping/etc.)
and record a simhash of its body text so Template detection can compare across
domains later.
"""
from __future__ import annotations

import hashlib
import re

from simhash import Simhash

from app.crawler.extractors.base import ExtractContext, ExtractorResult

_LEGAL_PATTERNS = {
    "privacy": re.compile(r"/(privacy|privacy[-_ ]policy)", re.I),
    "terms": re.compile(r"/(terms|tos|terms[-_ ]of[-_ ]service)", re.I),
    "refund": re.compile(r"/(refund|returns?|refund[-_ ]policy)", re.I),
    "shipping": re.compile(r"/(shipping|delivery)", re.I),
    "cookie": re.compile(r"/(cookie|cookies|cookie[-_ ]policy)", re.I),
}

_HEADER_HINTS = {
    "privacy": re.compile(r"\bprivacy(?:\s+policy)?\b", re.I),
    "terms":   re.compile(r"\bterms(?:\s+of\s+(?:service|use))?\b", re.I),
    "refund":  re.compile(r"\brefund(?:s|\s+policy)?\b|\breturns?\b", re.I),
    "shipping": re.compile(r"\bshipping(?:\s+policy)?\b|\bdelivery\b", re.I),
    "cookie":  re.compile(r"\bcookie(?:s|\s+policy)?\b", re.I),
}


def extract_legal_pages(ctx: ExtractContext) -> ExtractorResult:
    kind = None
    path = ctx.final_url.path or ""
    for k, pat in _LEGAL_PATTERNS.items():
        if pat.search(path):
            kind = k
            break
    if kind is None:
        # fallback: look at H1/H2 text
        heading = " ".join(h.get_text(" ", strip=True) for h in ctx.soup.find_all(["h1", "h2"])[:3])
        for k, pat in _HEADER_HINTS.items():
            if pat.search(heading):
                kind = k
                break
    if kind is None:
        return ExtractorResult()

    body = (ctx.extracted.get("readable_text") or "").strip()
    if not body:
        return ExtractorResult(extracted={"legal_page": {"kind": kind}})

    return ExtractorResult(
        extracted={
            "legal_page": {
                "kind": kind,
                "url": ctx.final_url.normalized,
                "text_len": len(body.split()),
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "simhash": Simhash(body).value,
            }
        }
    )
