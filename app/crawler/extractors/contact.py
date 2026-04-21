"""Contact detail extractor: emails, phone numbers, crude address candidates."""
from __future__ import annotations

import re

import phonenumbers

from app.crawler.extractors.base import ExtractContext, ExtractorResult

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _collect_emails(text: str, soup) -> list[str]:
    emails = set(_EMAIL_RE.findall(text))
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            emails.add(a["href"][7:].split("?")[0])
    return sorted(emails)[:20]


def _collect_phones(text: str) -> list[str]:
    out: set[str] = set()
    for region in ("US", "GB", "IN", "DE", "FR"):
        try:
            for m in phonenumbers.PhoneNumberMatcher(text, region):
                out.add(phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164))
        except Exception:
            continue
        if len(out) >= 10:
            break
    return sorted(out)[:10]


def extract_contacts(ctx: ExtractContext) -> ExtractorResult:
    text = (ctx.extracted.get("readable_text") or ctx.soup.get_text(" ", strip=True))[:50_000]
    return ExtractorResult(
        extracted={
            "contacts": {
                "emails": _collect_emails(text, ctx.soup),
                "phones": _collect_phones(text),
            }
        }
    )
