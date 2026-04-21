"""Tests for the Track 1 Report Writer (``app.reporter.writer``).

These tests exercise the validation layers WITHOUT hitting the Anthropic API.
The strategy is: call the internal ``_parse_and_validate`` function directly
with carefully-crafted LLM responses, and assert that legitimate outputs pass
while hallucinated ones are rejected.

We also test the public ``write_report`` to confirm the "degrade gracefully"
contract: no API key → returns None, API raises → returns None.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.aggregator import Finding, Verdict
from app.reporter import writer


# ---------------------------------------------------------------------------
# Helpers — build a Verdict that matches what the LLM will see
# ---------------------------------------------------------------------------


def _verdict_with_findings(findings: list[Finding], band: str = "high") -> Verdict:
    return Verdict(
        risk_band=band,
        confidence=0.85,
        summary="template summary",
        findings=findings,
        score=3.5,
        reason="threshold",
    )


def _good_response(v: Verdict) -> str:
    """A well-formed LLM response body (already has the prefilled "{" prepended)."""
    payload = {
        "headline": f"{v.risk_band.title()} risk detected on this site.",
        "why": "Multiple signals suggest the content on this page does not match its stated purpose.",
        "signal_explanations": [
            {
                "kind": f.kind,
                "plain_english": f"Concern related to {f.kind.split('.')[-1].replace('_', ' ')}.",
            }
            for f in v.findings
        ],
        "recommendation": "Do not enter credentials or payment details on this site.",
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Accept path
# ---------------------------------------------------------------------------


def test_well_formed_output_validates() -> None:
    v = _verdict_with_findings([
        Finding("crawl.canonical_origin_mismatch", "high", 0.9,
                "Canonical points to kohphanganrooms.com", 1.8),
        Finding("crawl.seed_vs_homepage_divergence", "high", 0.85,
                "Seed page differs from homepage", 1.7),
    ])
    narrative = writer._parse_and_validate(_good_response(v), v, "https://example.com/")
    assert narrative is not None
    assert narrative.headline.lower().startswith("high risk")
    assert len(narrative.signal_explanations) == 2
    assert narrative.signal_explanations[0].kind == "crawl.canonical_origin_mismatch"


def test_empty_findings_produces_valid_empty_explanations() -> None:
    v = _verdict_with_findings([], band="none")
    body = json.dumps({
        "headline": "No signals detected on this site.",
        "why": "No meaningful signals were recorded by the crawler.",
        "signal_explanations": [],
        "recommendation": "No risk signals found; ordinary caution applies.",
    })
    narrative = writer._parse_and_validate(body, v, "https://example.com/")
    assert narrative is not None
    assert narrative.signal_explanations == []


# ---------------------------------------------------------------------------
# Reject path — each hallucination mode gets its own test
# ---------------------------------------------------------------------------


def test_invalid_json_is_rejected() -> None:
    v = _verdict_with_findings([Finding("crawl.no_signal", "info", 0.5, "ok", 0)])
    assert writer._parse_and_validate("not json at all", v, "https://example.com/") is None


def test_missing_required_field_is_rejected() -> None:
    v = _verdict_with_findings([Finding("crawl.x.y", "high", 0.8, "s", 1.6)])
    body = json.dumps({
        "headline": "High risk",
        "why": "",  # empty → rejected
        "signal_explanations": [{"kind": "crawl.x.y", "plain_english": "..."}],
        "recommendation": "Do not proceed.",
    })
    assert writer._parse_and_validate(body, v, "https://example.com/") is None


def test_hallucinated_kind_is_rejected() -> None:
    """LLM invents a new finding.kind → reject."""
    v = _verdict_with_findings([
        Finding("crawl.canonical_origin_mismatch", "high", 0.9, "s", 1.8),
    ])
    body = json.dumps({
        "headline": "High risk on this site.",
        "why": "The canonical and a fake-kind-we-never-emitted both fired.",
        "signal_explanations": [
            {"kind": "crawl.canonical_origin_mismatch", "plain_english": "ok"},
            # This kind was NOT in the findings we provided. Classic hallucination.
            {"kind": "phishing.totally_made_up", "plain_english": "bad"},
        ],
        "recommendation": "Do not proceed.",
    })
    assert writer._parse_and_validate(body, v, "https://example.com/") is None


def test_reordered_kinds_are_rejected() -> None:
    """We require 1:1 same-order mapping — reorder detection is our strongest tripwire."""
    v = _verdict_with_findings([
        Finding("crawl.a", "high", 0.9, "first", 1.8),
        Finding("crawl.b", "high", 0.8, "second", 1.6),
    ])
    body = json.dumps({
        "headline": "High risk.",
        "why": "Various signals",
        "signal_explanations": [
            {"kind": "crawl.b", "plain_english": "..."},  # swapped
            {"kind": "crawl.a", "plain_english": "..."},
        ],
        "recommendation": "Do not proceed.",
    })
    assert writer._parse_and_validate(body, v, "https://example.com/") is None


def test_foreign_domain_in_narrative_is_rejected() -> None:
    """LLM mentions a domain we never provided → reject."""
    v = _verdict_with_findings([
        Finding("crawl.canonical_origin_mismatch", "high", 0.9,
                "Canonical points to legit.example", 1.8),
    ])
    body = json.dumps({
        "headline": "High risk on suspicious-scam-site.net.",  # hallucinated domain
        "why": "The canonical points at legit.example which is suspicious.",
        "signal_explanations": [
            {"kind": "crawl.canonical_origin_mismatch", "plain_english": "ok"},
        ],
        "recommendation": "Do not proceed.",
    })
    assert writer._parse_and_validate(body, v, "https://investigation.example/") is None


def test_allowed_domain_variants_pass() -> None:
    """www.example.com is fine if example.com was provided (and vice versa)."""
    v = _verdict_with_findings([
        Finding("crawl.canonical_origin_mismatch", "high", 0.9,
                "Canonical points to legit.example", 1.8),
    ])
    body = json.dumps({
        "headline": "High risk on www.investigation.example.",
        "why": "Canonical targets www.legit.example which differs from the page.",
        "signal_explanations": [
            {"kind": "crawl.canonical_origin_mismatch", "plain_english": "ok"},
        ],
        "recommendation": "Do not proceed.",
    })
    narrative = writer._parse_and_validate(body, v, "https://investigation.example/")
    assert narrative is not None


def test_verdict_band_mismatch_in_headline_is_rejected() -> None:
    """LLM says 'low risk' when we told it high → reject."""
    v = _verdict_with_findings([
        Finding("crawl.a", "high", 0.9, "s", 1.8),
    ], band="high")
    body = json.dumps({
        "headline": "Low risk detected on this site.",  # contradicts the verdict
        "why": "Some signals worth noting.",
        "signal_explanations": [
            {"kind": "crawl.a", "plain_english": "ok"},
        ],
        "recommendation": "Proceed with caution.",
    })
    assert writer._parse_and_validate(body, v, "https://example.com/") is None


def test_length_caps_enforced() -> None:
    v = _verdict_with_findings([Finding("crawl.a", "high", 0.9, "s", 1.8)])
    body = json.dumps({
        "headline": "H" * 500,  # way over cap
        "why": "ok",
        "signal_explanations": [{"kind": "crawl.a", "plain_english": "ok"}],
        "recommendation": "ok",
    })
    assert writer._parse_and_validate(body, v, "https://example.com/") is None


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_missing_api_key_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    v = _verdict_with_findings([Finding("crawl.a", "high", 0.9, "s", 1.8)])
    assert writer.write_report(v, "https://example.com/") is None


def test_api_error_returns_none(monkeypatch) -> None:
    """An exception from the anthropic SDK → None, never raised."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _FakeClient:
        def __init__(self, **_kw): self.messages = self

        def create(self, **_kw):
            raise RuntimeError("network failure")

    fake_mod = SimpleNamespace(Anthropic=_FakeClient)
    with patch.dict("sys.modules", {"anthropic": fake_mod}):
        v = _verdict_with_findings([Finding("crawl.a", "high", 0.9, "s", 1.8)])
        assert writer.write_report(v, "https://example.com/") is None


def test_happy_path_with_mocked_client(monkeypatch) -> None:
    """End-to-end: mocked anthropic call returns valid JSON → Narrative comes back."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    v = _verdict_with_findings([
        Finding("crawl.canonical_origin_mismatch", "high", 0.9,
                "Canonical points to elsewhere.example", 1.8),
    ])
    # The response is a complete JSON object. Older versions of writer.py used
    # an assistant-prefill turn and stripped the leading "{" from the mock,
    # but claude-sonnet-4.x rejects assistant-prefill, so the real API now
    # returns the full object and ``_isolate_json`` extracts it verbatim.
    response_body = json.dumps({
        "headline": "High risk detected on this site.",
        "why": "The page declares a canonical URL on a different domain.",
        "signal_explanations": [
            {"kind": "crawl.canonical_origin_mismatch", "plain_english": "Self-reference points elsewhere."},
        ],
        "recommendation": "Do not enter credentials on this site.",
    })

    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=response_body)]
    )

    class _FakeClient:
        def __init__(self, **_kw): self.messages = self
        def create(self, **_kw): return fake_resp

    fake_mod = SimpleNamespace(Anthropic=_FakeClient)
    with patch.dict("sys.modules", {"anthropic": fake_mod}):
        narrative = writer.write_report(v, "https://investigation.example/")
    assert narrative is not None
    assert narrative.source == "llm"
    assert narrative.model  # model name was stamped in
    assert len(narrative.signal_explanations) == 1
