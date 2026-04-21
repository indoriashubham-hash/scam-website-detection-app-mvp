"""Pulls title, meta description, canonical, OpenGraph/Twitter, favicon URL, generator.

Also emits ``crawl.canonical_origin_mismatch`` when the page's
``<link rel="canonical">`` points at a different registered domain — a strong
hijack/scam-template signal (e.g. wimberleymontessori.com/home.html whose
canonical points at a Thai nightlife site).
"""
from __future__ import annotations

from urllib.parse import urljoin

from app.crawler.extractors.base import ExtractContext, ExtractorResult, ProposedEvidence
from app.crawler.urls import normalize_url


def extract_metadata(ctx: ExtractContext) -> ExtractorResult:
    out: dict = {"title": ctx.title, "meta": {}}
    evidence: list[ProposedEvidence] = []
    soup = ctx.soup

    # canonical
    link_canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if link_canonical and link_canonical.get("href"):
        canonical = urljoin(ctx.final_url.normalized, link_canonical["href"])
        out["meta"]["canonical"] = canonical
        # Cross-origin canonical: if the canonical points at a different
        # registered domain than the page itself, the site is either hijacked,
        # running a scam template, or (rarely) legitimately mirroring content.
        # Either way, a human should look — so we surface it at HIGH severity.
        try:
            can = normalize_url(canonical)
            if (
                can.registered_domain
                and ctx.final_url.registered_domain
                and can.registered_domain != ctx.final_url.registered_domain
            ):
                evidence.append(
                    ProposedEvidence(
                        kind="crawl.canonical_origin_mismatch",
                        severity="high",
                        summary=(
                            f"<link rel=canonical> points to a different registered domain: "
                            f"{can.registered_domain} (page is on {ctx.final_url.registered_domain})"
                        ),
                        confidence=0.9,
                        details={
                            "page_url": ctx.final_url.normalized,
                            "page_domain": ctx.final_url.registered_domain,
                            "canonical": canonical,
                            "canonical_domain": can.registered_domain,
                        },
                    )
                )
        except Exception:
            # Malformed canonical href — ignore; normalize_url's strictness is
            # acceptable here because we're only opportunistically checking.
            pass

    # description
    for name in ("description", "og:description", "twitter:description"):
        el = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if el and el.get("content"):
            out["meta"].setdefault("description", el["content"])
            break

    # OpenGraph / Twitter
    og: dict = {}
    for el in soup.find_all("meta"):
        prop = el.get("property") or el.get("name")
        if not prop or not el.get("content"):
            continue
        if prop.startswith("og:") or prop.startswith("twitter:"):
            og[prop] = el["content"][:500]
    if og:
        out["meta"]["og"] = og

    # generator
    gen = soup.find("meta", attrs={"name": "generator"})
    if gen and gen.get("content"):
        out["meta"]["generator"] = gen["content"][:200]

    # favicon
    icon = (
        soup.find("link", rel=lambda v: v and "icon" in v)
        or soup.find("link", rel="shortcut icon")
    )
    if icon and icon.get("href"):
        out["favicon_url"] = urljoin(ctx.final_url.normalized, icon["href"])

    return ExtractorResult(extracted=out, evidence=evidence)
