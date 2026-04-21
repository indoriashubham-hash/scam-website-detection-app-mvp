"""Form extractor.

Flags three strong signals:
- password field that will POST to a different origin   -> critical
- password field over plain HTTP                        -> critical
- payment-looking form posting cross-origin             -> high
"""
from __future__ import annotations

from urllib.parse import urljoin

from app.crawler.extractors.base import ExtractContext, ExtractorResult, ProposedEvidence
from app.crawler.urls import normalize_url, same_origin

_LOGIN_FIELD_HINTS = ("pass", "pwd", "secret")
_PAYMENT_HINTS = ("cc-number", "cardnumber", "card-number", "cvc", "cvv", "card_cvc")


def _is_login(fields: list[dict]) -> bool:
    for f in fields:
        if f.get("type") == "password":
            return True
        if any(h in (f.get("name") or "").lower() for h in _LOGIN_FIELD_HINTS):
            return True
    return False


def _is_payment(fields: list[dict]) -> bool:
    hay = " ".join((f.get("autocomplete") or "") + " " + (f.get("name") or "") for f in fields).lower()
    return any(h in hay for h in _PAYMENT_HINTS)


def extract_forms(ctx: ExtractContext) -> ExtractorResult:
    out_forms: list[dict] = []
    evidence: list[ProposedEvidence] = []

    for form in ctx.soup.find_all("form"):
        method = (form.get("method") or "GET").upper()
        action_raw = form.get("action") or ctx.final_url.normalized
        action_abs = urljoin(ctx.final_url.normalized, action_raw)

        fields: list[dict] = []
        for inp in form.find_all(["input", "select", "textarea"]):
            fields.append(
                {
                    "name": inp.get("name"),
                    "type": (inp.get("type") or inp.name or "text").lower(),
                    "autocomplete": inp.get("autocomplete"),
                    "required": inp.has_attr("required"),
                }
            )

        is_login = _is_login(fields)
        is_payment = _is_payment(fields)

        posts_cross_origin = False
        try:
            parsed_action = normalize_url(action_abs)
            posts_cross_origin = not same_origin(parsed_action, ctx.final_url)
        except Exception:
            pass

        out_forms.append(
            {
                "action": action_abs,
                "method": method,
                "fields": fields,
                "is_login": is_login,
                "is_payment": is_payment,
                "posts_cross_origin": posts_cross_origin,
            }
        )

        if is_login and posts_cross_origin:
            evidence.append(
                ProposedEvidence(
                    kind="crawl.login_form_cross_origin_post",
                    severity="critical",
                    summary="Login form submits credentials to a different origin",
                    confidence=0.95,
                    details={"action": action_abs, "page_origin": ctx.final_url.origin},
                )
            )
        if is_login and ctx.final_url.scheme == "http":
            evidence.append(
                ProposedEvidence(
                    kind="crawl.password_field_over_http",
                    severity="critical",
                    summary="Password field served over plaintext HTTP",
                    confidence=0.99,
                    details={"url": ctx.final_url.normalized},
                )
            )
        if is_payment and posts_cross_origin:
            evidence.append(
                ProposedEvidence(
                    kind="crawl.payment_form_cross_origin_post",
                    severity="high",
                    summary="Payment form posts card data cross-origin",
                    confidence=0.9,
                    details={"action": action_abs},
                )
            )

    return ExtractorResult(extracted={"forms": out_forms}, forms=out_forms, evidence=evidence)
