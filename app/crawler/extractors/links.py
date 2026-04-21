"""Anchor extraction. Produces ProposedLink entries; the pipeline routes them to
``planner.offer()`` for same-origin URLs and persists them all as ``outlinks`` rows."""
from __future__ import annotations

from urllib.parse import urljoin

from app.crawler.extractors.base import ExtractContext, ExtractorResult, ProposedLink


def extract_links(ctx: ExtractContext) -> ExtractorResult:
    links: list[ProposedLink] = []
    for a in ctx.soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_href = urljoin(ctx.final_url.normalized, href)
        links.append(
            ProposedLink(
                href=abs_href,
                rel=a.get("rel")[0] if a.get("rel") else None,
                anchor_text=(a.get_text(" ", strip=True) or "")[:200] or None,
            )
        )
    return ExtractorResult(extracted={"link_count": len(links)}, links=links)
