"""URL normalization + classification helpers. Small but referenced everywhere.

Rules:
- Always normalize before hashing/deduping.
- `registered_domain` uses tldextract with fresh TLDs disabled (deterministic tests).
- `is_private_target` is the egress safety check.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import tldextract

_tld = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Well-known paths we always try (see design doc §4.2).
WELL_KNOWN_PATHS: tuple[str, ...] = (
    # trust
    "/about", "/about-us", "/contact", "/contact-us",
    "/terms", "/tos", "/terms-of-service",
    "/privacy", "/privacy-policy",
    "/shipping", "/returns", "/refund", "/refund-policy",
    "/faq", "/help", "/legal", "/imprint",
    # commerce
    "/cart", "/checkout", "/pricing", "/shop", "/store", "/products",
    # auth
    "/login", "/signin", "/sign-in", "/account", "/verify", "/password/reset",
)

TRUST_PATTERNS = re.compile(
    r"/(about|contact|terms|tos|privacy|shipping|returns?|refunds?|faq|help|legal|imprint)",
    re.I,
)
AUTH_PATTERNS = re.compile(r"/(login|sign[-_]?in|account|verify|password)", re.I)
COMMERCE_PATTERNS = re.compile(r"/(cart|checkout|pricing|product|shop|store)", re.I)


@dataclass(slots=True, frozen=True)
class ParsedUrl:
    original: str
    normalized: str
    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    registered_domain: str

    @property
    def origin(self) -> str:
        base = f"{self.scheme}://{self.host}"
        return f"{base}:{self.port}" if self.port else base


def normalize_url(url: str) -> ParsedUrl:
    """Normalize a URL for deduping & comparison.

    - Lower-case scheme + host
    - Strip fragment
    - Remove default ports (80/443)
    - Drop trailing '/' on path-only URLs
    - Preserve query (sometimes meaningful for search pages)
    """
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port: int | None = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    netloc = host if port is None else f"{host}:{port}"
    normalized = urlunsplit((scheme, netloc, path, parts.query, ""))
    ex = _tld(host)
    registered = ".".join(p for p in (ex.domain, ex.suffix) if p)
    return ParsedUrl(
        original=url,
        normalized=normalized,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=parts.query,
        registered_domain=registered,
    )


def is_private_target(host: str) -> bool:
    """Reject internal/private addresses — SSRF / cloud-metadata protection."""
    if not host:
        return True
    lowered = host.lower()
    # The AWS/GCP metadata addresses and localhost-style hosts.
    if lowered in {"localhost", "metadata.google.internal"}:
        return True
    try:
        # If DNS fails we err on the side of rejecting (safer than resolving mid-crawl).
        infos = socket.getaddrinfo(lowered, None)
    except OSError:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
        # AWS IMDS
        if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
            return True
    return False


def same_origin(a: ParsedUrl, b: ParsedUrl) -> bool:
    return (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)
