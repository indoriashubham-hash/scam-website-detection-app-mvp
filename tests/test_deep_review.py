"""Tests for the Deep Reviewer validator (``app.reporter.deep``).

These tests exercise ``_parse_and_validate`` directly with crafted JSON
bodies, the same strategy ``tests/test_reporter.py`` uses for Track 1.

We do NOT hit the Anthropic API. Every test passes the new Minto-style
schema (governing_thought / supporting_pillars / contradictions / caveats).
"""
from __future__ import annotations

import json

from app.aggregator import Finding, Verdict
from app.reporter import deep as deep_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verdict(findings: list[Finding], band: str = "high") -> Verdict:
    return Verdict(
        risk_band=band,
        confidence=0.85,
        summary="template summary",
        findings=findings,
        score=3.5,
        reason="threshold",
    )


def _allowed_sources() -> set[str]:
    return {
        "verdict",
        "seed_page_text",
        "homepage_text",
        "screenshot:seed",
        "screenshot:homepage",
        "finding:crawl.canonical_origin_mismatch",
        "finding:crawl.seed_vs_homepage_divergence",
    }


def _good_body() -> dict:
    return {
        "governing_thought": (
            "The page declares a canonical that points at a different site "
            "while its own content duplicates a third-party listing."
        ),
        "supporting_pillars": [
            {
                "claim": "The site's self-reference contradicts its displayed identity.",
                "evidence": [
                    {
                        "sources": ["finding:crawl.canonical_origin_mismatch", "seed_page_text"],
                        "text": "The canonical URL targets a different domain than the page is served from.",
                    },
                ],
            },
            {
                "claim": "The seed page and the homepage describe unrelated businesses.",
                "evidence": [
                    {
                        "sources": ["finding:crawl.seed_vs_homepage_divergence"],
                        "text": "The seed's topical content differs sharply from the homepage.",
                    },
                    {
                        "sources": ["homepage_text", "seed_page_text"],
                        "text": "The homepage and the seed page do not appear to share branding.",
                    },
                ],
            },
        ],
        "contradictions": [
            {
                "sources": ["homepage_text"],
                "text": "The homepage does load cleanly despite the mismatch.",
            },
        ],
        "caveats": [
            "The screenshots for the extra pages could not be examined in this review.",
        ],
    }


# ---------------------------------------------------------------------------
# Accept path
# ---------------------------------------------------------------------------


def test_well_formed_output_validates() -> None:
    v = _verdict([
        Finding("crawl.canonical_origin_mismatch", "high", 0.9, "Canonical points elsewhere", 1.8),
        Finding("crawl.seed_vs_homepage_divergence", "high", 0.85, "Seed differs from homepage", 1.7),
    ])
    body = json.dumps(_good_body())
    dr = deep_mod._parse_and_validate(
        body,
        verdict=v,
        url="https://example.com/path",
        allowed_sources=_allowed_sources(),
    )
    assert dr is not None
    assert dr.schema_version == 2
    assert len(dr.supporting_pillars) == 2
    assert dr.supporting_pillars[0].claim.startswith("The site's self-reference")
    assert dr.supporting_pillars[1].evidence[0].sources == [
        "finding:crawl.seed_vs_homepage_divergence"
    ]
    assert len(dr.contradictions) == 1
    assert dr.caveats == [
        "The screenshots for the extra pages could not be examined in this review."
    ]


def test_empty_contradictions_and_caveats_are_valid() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["contradictions"] = []
    body["caveats"] = []
    dr = deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    )
    assert dr is not None
    assert dr.contradictions == []
    assert dr.caveats == []


def test_singular_source_key_is_accepted() -> None:
    """Defense against LLMs occasionally emitting 'source' instead of 'sources'."""
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["supporting_pillars"][0]["evidence"][0] = {
        "source": "finding:crawl.canonical_origin_mismatch",
        "text": "The canonical URL targets a different domain than the page is served from.",
    }
    dr = deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    )
    assert dr is not None
    assert dr.supporting_pillars[0].evidence[0].sources == [
        "finding:crawl.canonical_origin_mismatch"
    ]


# ---------------------------------------------------------------------------
# Reject path
# ---------------------------------------------------------------------------


def test_invalid_json_is_rejected() -> None:
    v = _verdict([])
    assert deep_mod._parse_and_validate(
        "definitely not json",
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_missing_governing_thought_is_rejected() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["governing_thought"] = ""
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_one_pillar_is_rejected() -> None:
    """Minto requires at least 2 MECE pillars."""
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["supporting_pillars"] = [body["supporting_pillars"][0]]
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_five_pillars_are_rejected() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    pillar = body["supporting_pillars"][0]
    body["supporting_pillars"] = [pillar, pillar, pillar, pillar, pillar]
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_pillar_with_no_evidence_is_rejected() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["supporting_pillars"][0]["evidence"] = []
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_hallucinated_source_is_rejected() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["supporting_pillars"][0]["evidence"][0]["sources"] = [
        "finding:totally_made_up_kind"
    ]
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_foreign_domain_in_governing_thought_is_rejected() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["governing_thought"] = (
        "The site phishing-scam-site.net has multiple mismatched signals."
    )
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_contradicting_high_verdict_is_rejected() -> None:
    """If the verdict is high/critical, the deep review can't claim the site is safe."""
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)], band="high")
    body = _good_body()
    body["governing_thought"] = "This site is safe and has a clear identity."
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_recommendation_phrasing_is_rejected() -> None:
    """Track 2 must not give user-facing advice — that's Track 1's job."""
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["supporting_pillars"][0]["evidence"][0]["text"] = (
        "You should avoid entering credentials on this site."
    )
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


def test_caveat_over_length_cap_is_rejected() -> None:
    v = _verdict([Finding("crawl.canonical_origin_mismatch", "high", 0.9, "", 1.8)])
    body = _good_body()
    body["caveats"] = ["x" * 400]  # cap is 300
    assert deep_mod._parse_and_validate(
        json.dumps(body),
        verdict=v,
        url="https://example.com/",
        allowed_sources=_allowed_sources(),
    ) is None


# ---------------------------------------------------------------------------
# _isolate_json — robustness of the extractor that shields us from fence/preamble
# ---------------------------------------------------------------------------


def test_isolate_json_strips_code_fence() -> None:
    s = "```json\n{\"a\": 1}\n```"
    assert deep_mod._isolate_json(s) == '{"a": 1}'


def test_isolate_json_handles_nested_braces_inside_strings() -> None:
    s = '{"k": "value with } inside", "nested": {"n": 1}}'
    out = deep_mod._isolate_json(s)
    assert out == s
    # And it actually parses:
    assert json.loads(out)["nested"]["n"] == 1


def test_isolate_json_returns_none_for_no_object() -> None:
    assert deep_mod._isolate_json("no braces here") is None
    assert deep_mod._isolate_json("") is None
