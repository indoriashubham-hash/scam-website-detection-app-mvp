"""Controlled vocabulary for `evidence.kind`.

Rules:
- Every analyzer declares its kinds in this file.
- `KNOWN_KINDS` is the union. `EvidenceEmitter` rejects anything not in here.
- `kind` string shape: ``<analyzer>.<slug>`` — enforced by a regex in tests.
"""
from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# -----------------------------------------------------------------------------
# crawl.*       — produced by the crawler itself
# -----------------------------------------------------------------------------
CRAWL_KINDS: set[str] = {
    "crawl.plan",                           # informational: what we planned to crawl
    "crawl.robots_full_disallow",           # robots.txt blocks /
    "crawl.robots_partial_disallow",
    "crawl.sitemap_found",
    "crawl.sitemap_missing",
    "crawl.redirect_chain",
    "crawl.redirect_loop",
    "crawl.homepage_unreachable",
    "crawl.page_too_large",
    "crawl.js_runtime_errors",
    "crawl.captcha_blocked",
    "crawl.no_signal",                      # explicit "we saw nothing worth reporting"
    "crawl.login_form_cross_origin_post",   # strong phishing adjacent
    "crawl.password_field_over_http",       # cleartext credential collection
    "crawl.payment_form_cross_origin_post",
    "crawl.language_mismatch",
    "crawl.suspicious_ui_countdown",        # fake urgency
    "crawl.tls_invalid",
    # Seed-vs-homepage divergence — the user-submitted URL is the investigation
    # subject. If it materially differs from the site's real homepage, that's a
    # strong "hijacked subpage / malicious landing" signal that we must surface
    # even when the homepage looks clean.
    "crawl.seed_unreachable",               # the user's URL itself failed
    "crawl.seed_vs_homepage_divergence",    # seed page differs materially from /
    "crawl.seed_vs_homepage_similar",       # informational: they basically match
    # Anti-bot interstitials — when set, downstream extractors are looking at a
    # challenge page, not the real site. We must surface this so reviewers know
    # absence-of-signal is "we couldn't see", not "nothing to see".
    "crawl.bot_block_detected",
    # <link rel="canonical"> points to a different registered domain than the
    # page itself. Classic hijack/scam-template signal.
    "crawl.canonical_origin_mismatch",
    # An internal link on the site led to a page whose final origin is a
    # different registered domain. Real businesses rarely have in-site links
    # that silently leave their domain without clear labeling.
    "crawl.off_origin_page_in_site",
    # Multiple standard "trust" pages (About, Contact, FAQ, Terms, Privacy)
    # return HTTP 4xx when probed. Legitimate businesses have these.
    "crawl.nav_404_cluster",
    # Crawled pages use visibly different languages without a locale switcher
    # (e.g. English homepage but Thai or Russian inner pages). Classic signal
    # of content grafted onto an unrelated site.
    "crawl.language_mismatch_across_pages",
}

# future analyzers (stubbed so the vocab is stable)
INSITE_SEARCH_KINDS: set[str] = {
    "insite_search.native_search_detected",
    "insite_search.probe_hit",
    "insite_search.probe_miss",
    "insite_search.corpus_fallback_hit",
    "insite_search.no_signal",
}
TEMPLATE_KINDS: set[str] = {
    "template.duplicate_policy_page",
    "template.duplicate_product_description",
    "template.shared_contact_details",
    "template.no_signal",
}
PHISHING_KINDS: set[str] = {
    "phishing.brand_similarity",
    "phishing.logo_similarity",
    "phishing.known_kit_match",
    "phishing.no_signal",
}
REPUTATION_KINDS: set[str] = {
    "reputation.complaint_found",
    "reputation.news_mention",
    "reputation.no_signal",
}
INFRA_KINDS: set[str] = {
    "infra.dns_nxdomain",
    "infra.domain_age_young",
    "infra.registrar_privacy",
    "infra.shared_ip_with_many",
    "infra.tls_issuer_cheap",
    "infra.no_signal",
}

KNOWN_KINDS: set[str] = (
    CRAWL_KINDS | INSITE_SEARCH_KINDS | TEMPLATE_KINDS | PHISHING_KINDS | REPUTATION_KINDS | INFRA_KINDS
)
