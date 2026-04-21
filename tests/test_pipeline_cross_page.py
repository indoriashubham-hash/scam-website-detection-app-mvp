"""Unit tests for the two post-crawl, cross-page analyzers on CrawlPipeline:

* ``_emit_nav_404_cluster``      — crawl.nav_404_cluster
* ``_emit_language_mismatch_across_pages`` — crawl.language_mismatch_across_pages

These methods only read ``self._nav_404s`` / ``self._page_langs`` and call
``self.emitter.emit``. We skip full pipeline construction and inject a fake
emitter so the tests stay pure-Python (no DB, no planner, no browser).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.crawler.pipeline import CrawlPipeline


@dataclass
class _Emission:
    kind: str
    severity: Any
    summary: str
    confidence: float
    details: dict | None


@dataclass
class _FakeEmitter:
    emitted: list[_Emission] = field(default_factory=list)

    async def emit(self, *, kind, severity, summary, confidence=0.5,
                   details=None, page_id=None, screenshot_key=None) -> None:
        self.emitted.append(_Emission(kind, severity, summary, confidence, details))


def _bare_pipeline() -> CrawlPipeline:
    """Construct a pipeline without running __init__ — we don't need the
    session/planner/renderer/storage for these unit tests."""
    p = CrawlPipeline.__new__(CrawlPipeline)
    p._nav_404s = []
    p._page_langs = []
    p._emitted_any_evidence = False
    p.emitter = _FakeEmitter()
    return p


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# crawl.nav_404_cluster
# ---------------------------------------------------------------------------


def test_nav_404_cluster_single_404_does_not_emit() -> None:
    p = _bare_pipeline()
    p._nav_404s = [
        {"url": "https://x.example/about", "path": "/about", "status": 404, "source": "wellknown"},
    ]
    _run(p._emit_nav_404_cluster())
    assert p.emitter.emitted == []
    assert p._emitted_any_evidence is False


def test_nav_404_cluster_same_family_twice_does_not_emit() -> None:
    # /about and /about-us are the same trust family — a single topic failing
    # (even if it has two URL variants) shouldn't count as a cluster.
    p = _bare_pipeline()
    p._nav_404s = [
        {"url": "https://x.example/about", "path": "/about", "status": 404, "source": "wellknown"},
        {"url": "https://x.example/about-us", "path": "/about-us", "status": 404, "source": "wellknown"},
    ]
    _run(p._emit_nav_404_cluster())
    assert p.emitter.emitted == []


def test_nav_404_cluster_two_families_emits_medium() -> None:
    p = _bare_pipeline()
    p._nav_404s = [
        {"url": "https://x.example/about", "path": "/about", "status": 404, "source": "wellknown"},
        {"url": "https://x.example/contact", "path": "/contact", "status": 404, "source": "wellknown"},
    ]
    _run(p._emit_nav_404_cluster())
    assert len(p.emitter.emitted) == 1
    e = p.emitter.emitted[0]
    assert e.kind == "crawl.nav_404_cluster"
    assert e.severity.value == "medium"
    assert "2" in e.summary
    assert "about" in e.summary and "contact" in e.summary
    # deduped family list lives in details for reviewer cross-check
    assert sorted(e.details["families"]) == ["about", "contact"]
    assert p._emitted_any_evidence is True


def test_nav_404_cluster_plural_variants_collapse() -> None:
    # /returns and /return should collapse to one family (trailing 's' stripped).
    # Paired with /refund (a distinct family) that's 2 families total.
    p = _bare_pipeline()
    p._nav_404s = [
        {"url": "https://x.example/returns", "path": "/returns", "status": 404, "source": "wellknown"},
        {"url": "https://x.example/return", "path": "/return", "status": 404, "source": "wellknown"},
        {"url": "https://x.example/refund", "path": "/refund", "status": 404, "source": "wellknown"},
    ]
    _run(p._emit_nav_404_cluster())
    assert len(p.emitter.emitted) == 1
    assert sorted(p.emitter.emitted[0].details["families"]) == ["refund", "return"]


# ---------------------------------------------------------------------------
# crawl.language_mismatch_across_pages
# ---------------------------------------------------------------------------


def test_language_mismatch_single_page_does_not_emit() -> None:
    p = _bare_pipeline()
    p._page_langs = [("https://x.example/", "en")]
    _run(p._emit_language_mismatch_across_pages())
    assert p.emitter.emitted == []


def test_language_mismatch_same_language_does_not_emit() -> None:
    p = _bare_pipeline()
    p._page_langs = [
        ("https://x.example/", "en"),
        ("https://x.example/about", "en"),
        ("https://x.example/contact", "en-US"),  # regional variant collapses to en
    ]
    _run(p._emit_language_mismatch_across_pages())
    assert p.emitter.emitted == []


def test_language_mismatch_locale_hinted_urls_are_ignored() -> None:
    # /en/foo and /th/bar are explicitly labeled — a real multilingual site.
    # Those pages should NOT drive the mismatch.
    p = _bare_pipeline()
    p._page_langs = [
        ("https://x.example/en/about", "en"),
        ("https://x.example/th/about", "th"),
    ]
    _run(p._emit_language_mismatch_across_pages())
    assert p.emitter.emitted == []


def test_language_mismatch_query_param_hinted_urls_are_ignored() -> None:
    p = _bare_pipeline()
    p._page_langs = [
        ("https://x.example/about?lang=en", "en"),
        ("https://x.example/about?lang=th", "th"),
    ]
    _run(p._emit_language_mismatch_across_pages())
    assert p.emitter.emitted == []


def test_language_mismatch_unmarked_pages_emit_medium() -> None:
    # Nursery school homepage reads as English, inner pages read as Thai,
    # neither URL has a locale marker — the wimberleymontessori pattern.
    p = _bare_pipeline()
    p._page_langs = [
        ("https://x.example/", "en"),
        ("https://x.example/home.html", "en"),
        ("https://x.example/news/post-1", "th"),
        ("https://x.example/news/post-2", "th"),
    ]
    _run(p._emit_language_mismatch_across_pages())
    assert len(p.emitter.emitted) == 1
    e = p.emitter.emitted[0]
    assert e.kind == "crawl.language_mismatch_across_pages"
    assert e.severity.value == "medium"
    assert sorted(e.details["languages"]) == ["en", "th"]
    assert p._emitted_any_evidence is True


def test_language_mismatch_mixed_marked_and_unmarked_only_unmarked_count() -> None:
    # Two unmarked pages in different languages → still emits even though other
    # pages carry locale markers. The marked ones are filtered, the two unmarked
    # ones span two languages.
    p = _bare_pipeline()
    p._page_langs = [
        ("https://x.example/en/about", "en"),      # filtered
        ("https://x.example/th/about", "th"),      # filtered
        ("https://x.example/home", "en"),          # kept
        ("https://x.example/promo", "ru"),         # kept
    ]
    _run(p._emit_language_mismatch_across_pages())
    assert len(p.emitter.emitted) == 1
    assert sorted(p.emitter.emitted[0].details["languages"]) == ["en", "ru"]
