"""Unit tests for the deterministic Risk Aggregator.

These are pure-Python tests — no DB, no Playwright, no network. They exercise
the scoring rules and overrides against the known cases:

* No evidence or only info/no_signal → band = "none"
* Only a bot-block signal → band = "insufficient" (visibility override)
* The wimberleymontessori scam pattern → band = "high"
* One critical row → band = "critical"
* A lone HIGH at 0.8 conf → band = "medium" (uncorroborated)
* Two HIGHs corroborating → band = "high"

Treat these as the contract for what an aggregator change must preserve.
"""
from __future__ import annotations

from app.aggregator import aggregate


def _ev(kind: str, severity: str, confidence: float = 0.8, summary: str = "") -> dict:
    return {
        "kind": kind,
        "severity": severity,
        "confidence": confidence,
        "summary": summary or f"{kind} ({severity})",
    }


# ---------------------------------------------------------------------------
# Empty and no-signal cases
# ---------------------------------------------------------------------------


def test_empty_evidence_returns_none_with_low_confidence() -> None:
    v = aggregate([])
    assert v.risk_band == "none"
    assert v.reason == "no_evidence"
    assert v.findings == []
    assert v.confidence <= 0.4  # we saw literally nothing


def test_only_no_signal_rows_return_none_with_higher_confidence() -> None:
    # Analyzers ran and found nothing → more confident the site is clean than
    # the empty case (but not fully confident — we still might have missed).
    v = aggregate([
        _ev("crawl.no_signal", "info"),
        _ev("phishing.no_signal", "info"),
        _ev("infra.no_signal", "info"),
    ])
    assert v.risk_band == "none"
    assert v.reason == "no_evidence"
    assert v.findings == []
    assert 0.5 <= v.confidence <= 0.7


def test_info_only_rows_do_not_surface_as_findings() -> None:
    # seed_vs_homepage_similar is INFO — it's a positive observation but must
    # never appear as a "finding" that drove a band.
    v = aggregate([_ev("crawl.seed_vs_homepage_similar", "info", 1.0)])
    assert v.risk_band == "none"
    assert v.findings == []


def test_bookkeeping_only_run_does_not_claim_no_evidence_collected() -> None:
    # Reality check for the common "clean site" case: the pipeline emits
    # crawl.plan and sitemap bookkeeping rows (all INFO), no analyzer has
    # anything to say, and no .no_signal rows exist. The deterministic summary
    # must NOT claim "no evidence was collected" — we crawled, we just found
    # nothing risky. This was a real regression reported against
    # kohphanganrooms.com before the fix.
    v = aggregate([
        _ev("crawl.plan", "info", 1.0, "Planned 12 URLs"),
        _ev("crawl.sitemap_found", "info", 1.0, "Found 3 sitemap URLs"),
    ])
    assert v.risk_band == "none"
    assert v.reason == "no_evidence"
    assert "No evidence was collected" not in v.summary
    assert "crawl completed" in v.summary.lower()


# ---------------------------------------------------------------------------
# Visibility overrides
# ---------------------------------------------------------------------------


def test_bot_block_alone_returns_insufficient() -> None:
    v = aggregate([
        _ev("crawl.bot_block_detected", "medium", 0.95, "Cloudflare challenge"),
    ])
    assert v.risk_band == "insufficient"
    assert v.reason == "visibility_blocked"
    assert "blocked our crawler" in v.summary
    # The blocker itself is still in findings so the report can surface it.
    assert any(f.kind == "crawl.bot_block_detected" for f in v.findings)


def test_seed_unreachable_alone_returns_insufficient() -> None:
    v = aggregate([
        _ev("crawl.seed_unreachable", "high", 0.9, "Seed URL timed out"),
    ])
    assert v.risk_band == "insufficient"
    assert v.reason == "visibility_blocked"
    assert "did not load" in v.summary


def test_bot_block_does_not_override_when_real_signals_present() -> None:
    # If we got bot-blocked on some pages but also found strong evidence,
    # the verdict should reflect the real evidence, not the blocker.
    v = aggregate([
        _ev("crawl.bot_block_detected", "medium", 0.9),
        _ev("crawl.login_form_cross_origin_post", "critical", 0.95,
            "Password field posts to other-host.ru"),
    ])
    assert v.risk_band == "critical"
    assert v.reason == "threshold"


def test_bot_block_with_only_low_signals_still_overrides() -> None:
    # Visibility override should still kick in when the only other signals are
    # low/medium — a bot-blocked page is dominantly "we couldn't see", not
    # "mildly suspicious".
    v = aggregate([
        _ev("crawl.bot_block_detected", "medium", 0.9),
        _ev("crawl.language_mismatch", "low", 0.6),
    ])
    assert v.risk_band == "insufficient"


# ---------------------------------------------------------------------------
# Band thresholds
# ---------------------------------------------------------------------------


def test_single_critical_reaches_critical_band() -> None:
    v = aggregate([
        _ev("crawl.login_form_cross_origin_post", "critical", 1.0,
            "Cleartext credential post to untrusted domain"),
    ])
    assert v.risk_band == "critical"
    assert v.findings[0].kind == "crawl.login_form_cross_origin_post"
    assert v.confidence >= 0.9


def test_two_high_findings_corroborate_into_high() -> None:
    v = aggregate([
        _ev("crawl.canonical_origin_mismatch", "high", 0.9),
        _ev("crawl.seed_vs_homepage_divergence", "high", 0.85),
    ])
    # 2.0*0.9 + 2.0*0.85 = 1.8 + 1.7 = 3.5 → high
    assert v.risk_band == "high"
    assert 3.4 <= v.score <= 3.6


def test_lone_high_at_modest_confidence_is_medium() -> None:
    # A single HIGH at 0.8 conf = 1.6 — below the high threshold (3.0), above
    # the medium threshold (1.2). Uncorroborated HIGHs should not auto-promote.
    v = aggregate([
        _ev("crawl.canonical_origin_mismatch", "high", 0.8),
    ])
    assert v.risk_band == "medium"


def test_several_mediums_aggregate_to_medium_not_high() -> None:
    v = aggregate([
        _ev("crawl.suspicious_ui_countdown", "medium", 0.7),
        _ev("crawl.language_mismatch", "medium", 0.8),
        _ev("crawl.redirect_chain", "medium", 0.6),
    ])
    # 0.8*0.7 + 0.8*0.8 + 0.8*0.6 = 1.68 → medium
    assert v.risk_band == "medium"


def test_only_low_signals_fall_into_low_band() -> None:
    v = aggregate([
        _ev("crawl.redirect_chain", "low", 0.9),
        _ev("crawl.language_mismatch", "low", 0.7),
    ])
    # 0.2*0.9 + 0.2*0.7 = 0.32 → just past LOW threshold (0.3)
    assert v.risk_band == "low"


# ---------------------------------------------------------------------------
# Findings ranking and summary
# ---------------------------------------------------------------------------


def test_findings_are_ranked_by_score_desc() -> None:
    v = aggregate([
        _ev("a.low", "low", 0.9),
        _ev("a.high", "high", 0.9),
        _ev("a.medium", "medium", 0.9),
        _ev("a.critical", "critical", 0.9),
    ])
    kinds = [f.kind for f in v.findings]
    # Highest score first
    assert kinds[:2] == ["a.critical", "a.high"]


def test_findings_capped_at_five() -> None:
    rows = [_ev(f"crawl.kind_{i}", "medium", 0.9) for i in range(10)]
    v = aggregate(rows)
    assert len(v.findings) == 5


def test_summary_leads_with_top_finding() -> None:
    v = aggregate([
        _ev("crawl.canonical_origin_mismatch", "high", 0.9,
            "Canonical points to kohphanganrooms.com"),
        _ev("crawl.seed_vs_homepage_divergence", "high", 0.85,
            "Seed page body differs sharply from homepage"),
    ])
    assert "kohphanganrooms.com" in v.summary
    assert "High risk" in v.summary


# ---------------------------------------------------------------------------
# Regression: the wimberleymontessori.com scenario
# ---------------------------------------------------------------------------


def test_wimberleymontessori_scam_scenario_returns_high() -> None:
    """The real scam we caught: canonical points at a Thai casino, the seed
    page diverges hard from the homepage. Verdict must be at least HIGH and
    must cite the canonical mismatch as the strongest signal."""
    v = aggregate([
        _ev(
            "crawl.canonical_origin_mismatch", "high", 0.9,
            "Canonical URL points to kohphanganrooms.com (different domain)",
        ),
        _ev(
            "crawl.seed_vs_homepage_divergence", "high", 0.85,
            "Seed page has 106 words; homepage has 897 words",
        ),
        _ev("crawl.language_mismatch", "low", 0.6),
        _ev("crawl.no_signal", "info"),
    ])
    assert v.risk_band in ("high", "critical")
    assert v.findings[0].kind == "crawl.canonical_origin_mismatch"
    assert v.reason == "threshold"


def test_amazon_bot_blocked_scenario_returns_insufficient() -> None:
    """Amazon's anti-bot interstitial trips only bot_block_detected and a
    couple of low faint signals from the challenge page itself. Verdict must
    be ``insufficient``, never ``none`` (that would be wrong — we didn't see
    a clean site, we saw a wall)."""
    v = aggregate([
        _ev("crawl.bot_block_detected", "medium", 0.95,
            "Amazon 'Sorry! Something went wrong' interstitial"),
        _ev("crawl.redirect_chain", "low", 0.5),
    ])
    assert v.risk_band == "insufficient"
    assert v.reason == "visibility_blocked"
