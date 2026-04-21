"""Third-party script host collector.

We enumerate distinct external hosts of `<script src=...>` and match against a tiny
built-in list. This is a weak signal on its own; useful for template detection and
for confirming the site is actually wired to the services it claims to use.
"""
from __future__ import annotations

from urllib.parse import urlparse

from app.crawler.extractors.base import ExtractContext, ExtractorResult

_KNOWN = {
    "google-analytics.com": "google-analytics",
    "googletagmanager.com": "gtm",
    "facebook.net": "meta-pixel",
    "connect.facebook.net": "meta-pixel",
    "hotjar.com": "hotjar",
    "fullstory.com": "fullstory",
    "shopify.com": "shopify",
    "shopifycdn.com": "shopify",
    "wix.com": "wix",
    "squarespace.com": "squarespace",
    "wordpress.com": "wordpress",
    "cloudflareinsights.com": "cloudflare",
    "cdn.jsdelivr.net": "jsdelivr",
    "unpkg.com": "unpkg",
}


def extract_trackers(ctx: ExtractContext) -> ExtractorResult:
    hosts: set[str] = set()
    tags: set[str] = set()
    for s in ctx.soup.find_all("script", src=True):
        host = urlparse(s["src"]).hostname or ""
        host = host.lower()
        if not host or host == ctx.final_url.host:
            continue
        hosts.add(host)
        for known_host, tag in _KNOWN.items():
            if host == known_host or host.endswith("." + known_host):
                tags.add(tag)
    return ExtractorResult(
        extracted={"third_party_hosts": sorted(hosts)[:50], "platform_hints": sorted(tags)}
    )
