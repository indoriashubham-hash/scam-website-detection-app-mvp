"""Offline smoke tests — no Postgres / no browser. Catch obvious regressions in
extractor logic and URL handling."""
from __future__ import annotations

import re

import pytest

from app.crawler.extractors import DEFAULT_PIPELINE, run_pipeline
from app.crawler.extractors.base import make_context
from app.crawler.urls import is_private_target, normalize_url
from app.crawler.vocabulary import KNOWN_KINDS

_KIND_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def test_vocabulary_kinds_are_wellformed() -> None:
    for k in KNOWN_KINDS:
        assert _KIND_RE.match(k), f"bad evidence kind: {k!r}"


def test_normalize_url_strips_default_port_and_fragment() -> None:
    u = normalize_url("HTTP://Example.com:80/foo/#frag")
    assert u.scheme == "http"
    assert u.host == "example.com"
    assert u.port is None
    assert u.normalized == "http://example.com/foo"


def test_is_private_target_rejects_metadata_and_loopback() -> None:
    assert is_private_target("localhost") is True
    # We can't reliably resolve private CIDRs in CI without network; host-based check
    # already returns True for these well-knowns.
    assert is_private_target("metadata.google.internal") is True


HTML_SAFE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="description" content="Example description">
  <link rel="canonical" href="/home">
  <link rel="icon" href="/favicon.ico">
  <title>Nice Shop</title>
</head>
<body>
  <h1>Welcome to Nice Shop</h1>
  <p>We sell fine things. Contact us at hello@niceshop.example or +1 415-555-0100.</p>
  <form action="/login" method="post">
    <input name="user" type="text">
    <input name="password" type="password">
    <button>Sign in</button>
  </form>
  <a href="/about">About</a>
  <a href="https://other.example/ext">external</a>
</body></html>
"""


def test_extract_pipeline_produces_expected_fields() -> None:
    ctx = make_context(
        page_url="https://niceshop.example/",
        final_url="https://niceshop.example/",
        html=HTML_SAFE,
        title="Nice Shop",
        status=200,
        mime="text/html",
        cookies=[],
        console_errors=[],
    )
    r = run_pipeline(ctx, DEFAULT_PIPELINE)
    ex = r.extracted
    assert ex["title"] == "Nice Shop"
    assert ex["meta"]["description"] == "Example description"
    assert ex["meta"]["canonical"].endswith("/home")
    assert ex["favicon_url"].endswith("/favicon.ico")
    assert "readable_text" in ex
    assert ex["readable_text_len"] > 0
    assert ex["lang_declared"] == "en"

    # same-origin login form, not cross-origin → no critical evidence
    assert not any(e.kind == "crawl.login_form_cross_origin_post" for e in r.evidence)
    # but the form is captured
    assert any(f.get("is_login") for f in r.forms)

    # at least one link
    hrefs = [link.href for link in r.links]
    assert any(h.endswith("/about") for h in hrefs)


HTML_PHISHY = """
<!doctype html>
<html lang="en">
<body>
  <form action="https://other-host.ru/collect" method="post">
    <input name="user" type="text">
    <input name="pass" type="password">
  </form>
</body></html>
"""


def test_cross_origin_login_emits_critical_evidence() -> None:
    ctx = make_context(
        page_url="https://legit.example/",
        final_url="https://legit.example/",
        html=HTML_PHISHY,
        title="",
        status=200,
        mime="text/html",
        cookies=[],
        console_errors=[],
    )
    r = run_pipeline(ctx, DEFAULT_PIPELINE)
    kinds = {e.kind for e in r.evidence}
    assert "crawl.login_form_cross_origin_post" in kinds
    critical = [e for e in r.evidence if e.kind == "crawl.login_form_cross_origin_post"]
    assert critical and critical[0].severity == "critical"


HTML_CLOUDFLARE = """
<!doctype html>
<html><head><title>Just a moment...</title></head>
<body><div id="cf-browser-verification">Checking your browser before accessing the site.</div>
<p>Ray ID: abc123</p></body></html>
"""


def test_bot_block_detects_cloudflare_interstitial() -> None:
    from app.crawler.extractors.bot_block import extract_bot_block

    ctx = make_context(
        page_url="https://example.com/",
        final_url="https://example.com/",
        html=HTML_CLOUDFLARE,
        title="Just a moment...",
        status=503,
        mime="text/html",
        cookies=[],
        console_errors=[],
    )
    r = extract_bot_block(ctx)
    assert r.extracted["bot_block"] is True
    assert r.extracted["bot_block_provider"] == "cloudflare"
    kinds = [e.kind for e in r.evidence]
    assert "crawl.bot_block_detected" in kinds
    assert r.evidence[0].confidence >= 0.9  # 503 + marker → high confidence


def test_simhash_fits_in_signed_int64() -> None:
    # Regression for the wimberleymontessori.com crash: simhash returned
    # 17846560846600830425 which overflows Postgres BIGINT. After the fix,
    # visible_text must emit a value that fits in signed int64.
    from app.crawler.extractors.visible_text import extract_visible_text

    # Use long, varied text so Simhash bits fill out and can plausibly
    # exceed 2^63. Repeating the payload raises the odds of hitting the
    # high-bit case.
    long_text = (
        "<html><body>"
        + ("Starlight princess nightlife festival promotion discount offer " * 200)
        + "</body></html>"
    )
    ctx = make_context(
        page_url="https://x.example/",
        final_url="https://x.example/",
        html=long_text,
        title="x",
        status=200,
        mime="text/html",
        cookies=[],
        console_errors=[],
    )
    r = extract_visible_text(ctx)
    sh = r.extracted["simhash"]
    assert sh is not None
    assert -(1 << 63) <= sh <= (1 << 63) - 1, f"simhash {sh} out of signed int64 range"


def test_canonical_origin_mismatch_emits_high_severity() -> None:
    # Regression for the wimberleymontessori.com case: the scam page's
    # <link rel=canonical> pointed at kohphanganrooms.com. That mismatch must
    # fire a high-severity finding.
    html = """
    <!doctype html>
    <html>
      <head>
        <link rel="canonical" href="https://www.kohphanganrooms.com/Nightlife/HalfMoonFestival">
        <title>Montessori</title>
      </head>
      <body><p>irrelevant</p></body>
    </html>
    """
    from app.crawler.extractors.metadata import extract_metadata

    ctx = make_context(
        page_url="https://www.wimberleymontessori.com/home.html",
        final_url="https://www.wimberleymontessori.com/home.html",
        html=html,
        title="Montessori",
        status=200,
        mime="text/html",
        cookies=[],
        console_errors=[],
    )
    r = extract_metadata(ctx)
    kinds = {e.kind: e for e in r.evidence}
    assert "crawl.canonical_origin_mismatch" in kinds
    ev = kinds["crawl.canonical_origin_mismatch"]
    assert ev.severity == "high"
    assert ev.details["page_domain"] == "wimberleymontessori.com"
    assert ev.details["canonical_domain"] == "kohphanganrooms.com"


def test_canonical_same_origin_no_evidence() -> None:
    html = """
    <!doctype html><html><head>
      <link rel="canonical" href="https://example.com/home">
    </head><body></body></html>
    """
    from app.crawler.extractors.metadata import extract_metadata

    ctx = make_context(
        page_url="https://example.com/",
        final_url="https://example.com/",
        html=html, title="", status=200, mime="text/html",
        cookies=[], console_errors=[],
    )
    r = extract_metadata(ctx)
    assert not any(e.kind == "crawl.canonical_origin_mismatch" for e in r.evidence)


def test_bot_block_no_signal_on_normal_page() -> None:
    from app.crawler.extractors.bot_block import extract_bot_block

    ctx = make_context(
        page_url="https://example.com/",
        final_url="https://example.com/",
        html=HTML_SAFE,
        title="Nice Shop",
        status=200,
        mime="text/html",
        cookies=[],
        console_errors=[],
    )
    r = extract_bot_block(ctx)
    assert r.extracted["bot_block"] is False
    assert not r.evidence


def test_seed_priority_beats_sitemap_root(monkeypatch) -> None:
    # Regression test for the wimberleymontessori.com/home.html case:
    # the user-submitted URL must rank first in the frontier, even when the
    # sitemap offers a nicely-scoring root path.
    from unittest.mock import MagicMock

    from app.crawler import planner as planner_mod
    from app.crawler.planner import (
        HOMEPAGE_COMPARE_PRIORITY,
        SEED_PRIORITY,
        Planner,
    )

    # In a sandbox without DNS, is_private_target rejects everything. Stub it
    # for this test — we only care about priority ordering, not egress safety.
    monkeypatch.setattr(planner_mod, "is_private_target", lambda host: False)

    client = MagicMock()
    p = Planner("https://example.com/home.html", client)
    p._seed_frontier()
    # pop highest-priority item — must be the seed (source="seed")
    first = p.next()
    assert first is not None
    assert first.source == "seed", (
        f"expected seed first, got {first.source} (priority={first.priority})"
    )
    assert first.priority == SEED_PRIORITY
    # the root should be queued next, at the comparison slot
    second = p.next()
    assert second is not None
    assert second.source == "homepage_compare"
    assert second.priority == HOMEPAGE_COMPARE_PRIORITY


if __name__ == "__main__":  # convenience: `python tests/test_smoke.py`
    pytest.main([__file__, "-q"])
