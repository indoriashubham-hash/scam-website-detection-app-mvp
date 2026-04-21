# Website Risk Investigator — Architecture & Crawler Design

**Status:** v0.1 design doc
**Stack decision:** Python 3.11 + Playwright, Postgres, Redis, S3-compatible object store, Docker Compose (local-first)
**Author note:** This doc leads with the answer, then the reasoning, then the implementation detail. The Crawling module is covered at maximum depth; other modules are scoped but not yet designed in full.

---

## 1. Executive Answer (Minto)

**Build this as a queue-driven, evidence-first investigation engine — not a classifier.** A single `InvestigationJob` fans out into independent analyzer stages (Crawl, In-site Search, Reverse/Template, Phishing/Impersonation, Reputation, Infrastructure). Every finding is persisted as a typed `Evidence` row with a screenshot, a DOM snapshot, a hash, and a confidence score. A Reporting stage composes the executive narrative from those evidence rows. Nothing ships in the report without an evidence id.

**Three reasons this shape wins:**

1. **Auditability is the product.** A scam-investigation tool that can't show its work is a worse UX than a good search engine plus a curious human. The data model must make "explain why" free.
2. **Analyzers are independently evolvable.** Phishing detection, template detection, and infra checks each have their own research roadmaps. A shared job orchestrator with typed evidence keeps them decoupled and swappable.
3. **Crawling is the single point of leverage.** 80% of the signal — content, forms, login flows, legal pages, search behaviour, screenshots, duplicate text — is produced by the crawler. If the crawler is precise, polite, and deterministic, every downstream analyzer gets easier. If it's sloppy, every downstream analyzer is fighting noise.

**What this document covers:**

- System architecture (§2) and data model (§3)
- **Deep design of the Crawler module** (§4) — your priority
- Interfaces the crawler exposes to other analyzers (§5)
- Build plan with phased milestones (§6)
- Risks, trade-offs, and what I am deliberately not doing in v1 (§7)

---

## 2. System Architecture

### 2.1 High-level topology

```
┌──────────────┐    POST /investigations    ┌──────────────────┐
│   Web UI     │ ─────────────────────────▶ │   API (FastAPI)  │
│  (Next.js)   │ ◀──────────────────────── │                  │
└──────────────┘    GET /investigations/:id └────────┬─────────┘
                                                     │
                                           enqueues jobs
                                                     ▼
                                        ┌────────────────────────┐
                                        │   Redis + RQ / Celery  │
                                        │   (broker + scheduler) │
                                        └───────────┬────────────┘
                                                    │
    ┌───────────────┬────────────────┬──────────────┼────────────────┬─────────────────┐
    ▼               ▼                ▼              ▼                ▼                 ▼
┌─────────┐    ┌──────────┐    ┌────────────┐  ┌──────────┐   ┌──────────────┐   ┌──────────┐
│ Crawler │    │ Insite   │    │ Reverse/   │  │ Phishing │   │ Reputation   │   │ Infra/   │
│ Workers │    │ Search   │    │ Template   │  │ Impers.  │   │ (news/social)│   │ DNS/TLS  │
└────┬────┘    └────┬─────┘    └─────┬──────┘  └────┬─────┘   └──────┬───────┘   └────┬─────┘
     │              │                │              │                │                │
     └──────┬───────┴────────────────┴──────────────┴────────────────┴────────────────┘
            ▼
     ┌─────────────────┐   ┌──────────────────┐   ┌───────────────────┐
     │  Postgres (OLTP)│   │  S3 / MinIO      │   │  Reporting Worker │
     │  investigations │   │  screenshots,    │   │  (LLM + template) │
     │  pages, evidence│   │  HTML, raw bytes │   │                   │
     └─────────────────┘   └──────────────────┘   └───────────────────┘
```

### 2.2 Why these choices

- **FastAPI** — typed, async, OpenAPI for free; the API is thin (create/read jobs + stream events).
- **Redis + RQ (or Celery)** — RQ is simpler for a single team; Celery if you expect multi-region workers. Either way, the queue is the contract between stages.
- **Postgres as the system of record** — relational evidence with JSONB payloads gives you both auditability and flexible schemas per evidence type.
- **Object store (S3 or MinIO locally)** — screenshots, raw HTML, HAR files, DOM snapshots. These are large and immutable; never put them in Postgres.
- **Playwright (Chromium)** over Selenium or `requests` — modern sites are JS-heavy; you need a real browser for screenshots, search boxes, and login flows. `requests`/`httpx` is kept as a fast path for `robots.txt`, `sitemap.xml`, RSS, and static HTML shortcuts.
- **Docker Compose for local** — api, worker, postgres, redis, minio, playwright-base image. One `docker compose up` to run the whole product.

### 2.3 Non-functional targets (v1)

| Concern | Target |
|---|---|
| Time to full report (p50) | < 4 minutes |
| Time to full report (p95) | < 10 minutes |
| Concurrent investigations | 20 per host in v1 |
| Crawler politeness | Respect `robots.txt`, 1 req/sec/host default, configurable |
| Evidence retention | 90 days hot, S3 lifecycle → cold |
| Reproducibility | Every evidence row has `fetched_at`, `user_agent`, `ip`, `commit_sha` of analyzer |

---

## 3. Data Model (core tables)

```sql
investigations (
  id UUID PK,
  input_url TEXT,
  normalized_origin TEXT,       -- scheme+host+port
  status TEXT,                  -- queued|crawling|analyzing|reporting|done|failed
  risk_band TEXT,               -- low|medium|high|critical (filled by reporter)
  confidence NUMERIC,           -- 0..1
  created_at, updated_at, completed_at
)

pages (
  id UUID PK,
  investigation_id UUID FK,
  url TEXT, final_url TEXT,
  http_status INT, mime TEXT,
  title TEXT, lang TEXT,
  content_hash TEXT,            -- sha256 of normalized text
  simhash BIGINT,               -- for near-duplicate detection
  word_count INT,
  render_mode TEXT,             -- http|playwright
  fetched_at TIMESTAMPTZ,
  screenshot_key TEXT,          -- S3 key
  html_key TEXT, har_key TEXT,
  extracted JSONB               -- see §4.6
)

forms (
  id UUID PK, page_id UUID FK,
  action TEXT, method TEXT,
  fields JSONB,                 -- [{name,type,autocomplete,required}]
  is_login BOOL, is_payment BOOL,
  posts_cross_origin BOOL
)

outlinks (
  id UUID PK, page_id UUID FK,
  href TEXT, rel TEXT, anchor_text TEXT,
  same_origin BOOL, registered_domain TEXT
)

evidence (
  id UUID PK,
  investigation_id UUID FK,
  analyzer TEXT,                -- crawl|insite_search|template|phishing|reputation|infra
  kind TEXT,                    -- e.g. "login_form_cross_origin_post", "near_duplicate_policy"
  severity TEXT,                -- info|low|medium|high|critical
  confidence NUMERIC,
  summary TEXT,                 -- human-readable one-liner
  details JSONB,                -- analyzer-specific payload
  screenshot_key TEXT,          -- optional
  page_id UUID NULL FK,
  created_at TIMESTAMPTZ
)
```

**Design note:** `evidence.kind` is a controlled vocabulary. Every analyzer declares its kinds up front; the Reporter maps them to narrative templates. This is how you avoid a free-text blob and keep the report composable.

---

## 4. Crawler Module — Deep Design (priority)

### 4.1 Answer first

**The crawler is a bounded, breadth-first, priority-driven fetcher that renders with Playwright, extracts with a pluggable extractor pipeline, and emits three things per page: a `pages` row, a `screenshot`, and zero-or-more `evidence` rows.** It has seven sub-components: **Planner, Fetcher, Renderer, Extractor, Search Executor, Evidence Emitter, Storage.** Each is small, testable, and independently replaceable.

### 4.2 Crawl plan — what pages do we actually want?

Not "everything". A risk investigation cares about a specific subset:

1. **Homepage** (always).
2. **Canonical trust pages**: `/about`, `/contact`, `/terms`, `/privacy`, `/shipping`, `/refund`, `/returns`, `/faq`, `/legal`, `/imprint`.
3. **Commerce signal pages**: `/cart`, `/checkout`, `/pricing`, any page with `product`/`shop`/`store` in the path.
4. **Auth signal pages**: `/login`, `/signin`, `/account`, `/verify`, `/reset`.
5. **Sitemap-declared pages** — up to N per section, prioritized by priority and lastmod.
6. **Top-K in-site search results** for each high-risk keyword category (§4.7).

We cap at **~40 pages** per investigation in v1. Going wider adds cost without marginal signal. A follow-up "deep crawl" mode can be opt-in.

**Priority scoring for the frontier queue:**

```
priority = (is_homepage ? 100 : 0)
         + (matches_trust_page_pattern ? 60 : 0)
         + (matches_auth_pattern ? 80 : 0)
         + (matches_checkout_pattern ? 50 : 0)
         + (sitemap_priority * 20)
         + (link_depth == 1 ? 30 : link_depth == 2 ? 10 : 0)
         - (already_seen_similar_simhash ? 80 : 0)
```

### 4.3 Planner

**Responsibilities:**

- Normalize the input URL (strip fragments, lowercase host, resolve IDN/punycode, record both forms).
- Resolve redirects with a cheap HEAD/GET via `httpx`, record the redirect chain as evidence.
- Fetch `robots.txt`; parse crawl-delay and disallow rules.
- Fetch `/sitemap.xml`, `/sitemap_index.xml`, and any sitemaps referenced in robots. Parse nested indexes. Record the sitemap_count and stats.
- Seed the frontier with: homepage + trust/auth/commerce well-known paths (HEAD first to filter 404s) + top-N sitemap URLs by priority.
- Maintain a **per-origin rate limit** (default 1 rps, honored crawl-delay if higher).
- Emit a `CrawlPlan` evidence row with: sitemap found/not, robots.txt disallows, redirect chain, planned URLs.

**Rule:** the planner is the only thing that decides what to crawl. The fetcher/renderer never discovers new URLs on its own; it calls `planner.offer(url, source, priority)` and the planner decides. This keeps budgets enforceable.

### 4.4 Fetcher + Renderer

**Two-tier fetching** (speed + fidelity):

- **Tier 1 — httpx (static fetch):** used for robots, sitemaps, favicon, well-known JSON endpoints, `mailto`/`tel` probing. Fast, no browser overhead.
- **Tier 2 — Playwright (rendered fetch):** used for every page that becomes a `pages` row. Records: final URL after JS redirects, full DOM, HAR (network log), console errors, full-page screenshot, above-the-fold screenshot.

**Playwright configuration:**

```python
browser = chromium.launch(args=[
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
])
context = browser.new_context(
    user_agent=UA,                        # configurable, identifiable
    locale="en-US",
    viewport={"width": 1366, "height": 900},
    java_script_enabled=True,
    ignore_https_errors=False,            # we WANT to see cert errors as evidence
    record_har_path=f"{run_dir}/{page_id}.har",
    record_video_dir=None,                # video is expensive; skip in v1
)
```

**What we capture per page:**

| Artifact | Storage | Why |
|---|---|---|
| Full-page PNG screenshot | S3 | Visual evidence |
| Above-the-fold PNG | S3 | For the report gallery |
| Rendered HTML | S3 | Reverse-search, template match |
| HAR | S3 | Third-party requests, tracker fingerprint, cross-origin POSTs |
| Console errors | Postgres JSONB | Sloppy site signal |
| Final URL + redirect chain | Postgres | Phishing signal |
| TLS cert chain | Postgres JSONB | Infra analyzer input |
| Cookies set | Postgres JSONB | Tracker / analytics fingerprint |

**Politeness & safety controls:**

- Global max concurrency per host = 2.
- Per-investigation timeout (hard): 8 minutes.
- Per-page timeout: 30s navigation, 15s idle.
- Download blocklist: reject `application/octet-stream`, `.exe`, `.apk`, `.dmg`, `.zip` > 2MB, all `.iso`. Record as evidence but do not store the bytes.
- **Never submit forms. Never click buttons whose visible text looks like "buy", "checkout", "pay", "confirm".** Only read. The one exception is the site's search box (§4.7), where we submit a benign query.
- Egress isolation: crawl workers run in a network namespace that blocks internal/private CIDRs. A user typing `http://192.168.1.1` or an AWS metadata URL gets hard-rejected at the fetch layer.

### 4.5 Extractor pipeline

Each rendered page runs through an ordered set of extractors. Each extractor is a pure function `(page_context) -> partial[extracted]` that writes into the `pages.extracted` JSONB.

| Extractor | What it produces |
|---|---|
| `MetadataExtractor` | title, meta description, canonical, OpenGraph, Twitter card, favicon URL, generator tag |
| `VisibleTextExtractor` | readable body text via `trafilatura` with a `readability-lxml` fallback; stripped nav/footer |
| `ContactExtractor` | emails (regex + mailto), phone numbers (libphonenumber), physical addresses (regex + lightweight NER) |
| `LegalPageExtractor` | detects TOS/Privacy/Refund pages by URL, heading, and keyword density; extracts the raw policy text for later template match |
| `FormExtractor` | every `<form>` with action, method, fields, autocomplete attrs; flags `is_login` (password field + typical names), `is_payment` (card fields or payment keywords), `posts_cross_origin` |
| `LinkExtractor` | internal vs external links, `rel` attributes, registered domain (eTLD+1 via `tldextract`), link-to-brand ratio |
| `BrandAssetExtractor` | all logo-candidate `<img>`/`<svg>` from header/top region; downloads and computes pHash for impersonation analyzer |
| `ScriptTrackerExtractor` | enumerates third-party script hosts; matches against a known trackers list |
| `LanguageExtractor` | `lang` attribute + `langdetect` on body; flags language-vs-claimed-market mismatches |
| `SuspiciousUIExtractor` | detects countdown timers, fake "X people viewing now", chat-bubble impostors, fake trust badges via image hash matching |

**Design principle:** an extractor can emit `evidence` directly when its finding is strong (e.g., `FormExtractor` emits a `login_form_posts_cross_origin` evidence with severity=high). Weak signals stay in `pages.extracted` and are synthesized later by analyzers.

### 4.6 Example `pages.extracted` shape

```json
{
  "title": "SecureBank — Online Banking",
  "meta": {"description": "...", "canonical": "https://..."},
  "favicon_url": "https://.../favicon.ico",
  "favicon_phash": "0xabc123...",
  "readable_text_len": 3241,
  "lang_declared": "en",
  "lang_detected": "en",
  "contacts": {
    "emails": ["support@sec-bank-login.tk"],
    "phones": ["+1 415 555 0112"],
    "addresses": []
  },
  "legal_pages": [
    {"kind": "privacy", "url": "/privacy", "text_len": 1820, "simhash": 1234567890}
  ],
  "forms": [
    {"is_login": true, "action": "https://other-host.ru/collect", "posts_cross_origin": true,
     "fields": [{"name":"user","type":"text"},{"name":"pass","type":"password"}]}
  ],
  "brand_assets": [
    {"role":"logo","src":"/logo.png","phash":"0xdeadbeef..."}
  ],
  "suspicious_ui": {"fake_trust_badges": [], "countdown": false},
  "trackers": ["google-analytics.com","facebook.net"]
}
```

### 4.7 In-site Search Executor

This is the part most people get wrong; it deserves its own spec.

**Goal:** verify whether the site itself exposes high-risk content that wouldn't appear on the homepage.

**Algorithm:**

1. **Detect a native search surface.** In order of preference:
    - A `<form role="search">` or a form with `action` containing `search`/`query`.
    - A search `<input>` with `type=search` or `name in {q,query,s,search}`.
    - A JSON endpoint declared via `<link rel="search" type="application/opensearchdescription+xml">`.
    - A sitemap-declared search URL pattern.
2. **Classify the search mechanism** as GET (URL-templatable) vs JS-driven (requires Playwright interaction).
3. **For each high-risk category, submit one probe query**. Categories and *example* probes (kept intentionally generic; final list is config-driven and reviewed):
    - Counterfeit/scam-store: brand-name + "cheap", "wholesale"
    - Controlled substances (generic pharma names only; **no synthesis terms**)
    - Adult: category nouns, age-restricted terms
    - Fraud facilitation: "fake id", "bank logs", "cvv"
    - Phishing-adjacent: "login", "verify account"
4. **Capture evidence per probe:**
    - Screenshot of the search results view.
    - URL of the results page.
    - Count of returned results (parsed heuristically from counters or DOM result cards).
    - Top-5 result titles/URLs/snippets.
    - A `search_probe` evidence row with category, probe term, hit count, and severity by category.
5. **If there is no native search**, fall back to the **corpus-search mode**: run the probes against the crawled text corpus with a local index (Whoosh or sqlite FTS5). Clearly label the evidence as `corpus_search` not `site_search` — the distinction matters in the report.

**Evidence annotation:** screenshots are saved with a burnt-in label band (e.g., "In-site search for: `<term>` — 47 results") so they're self-explanatory when embedded in the report. Implementation: grab the screenshot, use Pillow to add a label bar.

**Safety rails for probes:**

- Probe list is signed/reviewed and loaded from a YAML config, not from user input.
- Each probe runs at most once per investigation.
- Probes that would type into a payment or auth field are statically excluded at config-load time.

### 4.8 Evidence Emitter & Storage

A thin service with a single API:

```python
def emit(
    investigation_id: UUID,
    analyzer: str,
    kind: str,
    severity: Severity,
    confidence: float,
    summary: str,
    details: dict,
    page_id: UUID | None = None,
    screenshot_key: str | None = None,
) -> UUID: ...
```

Every analyzer talks to this. This is also the single place where we enforce the controlled vocabulary for `kind`. Unknown kinds raise in tests and log a warning in prod — not a hard fail, because we want analyzers to evolve.

### 4.9 Concurrency model inside the crawler

- One investigation = one Playwright **browser context** (not a whole browser), reused across pages. Cleared cookies between origins.
- Page-level concurrency within one investigation = 2.
- Worker-level concurrency = one Playwright browser, many contexts (one per investigation), capped at N=4 per worker pod.
- Fail-isolation: a page timeout kills the page, not the context; a context crash kills the context, not the browser.

### 4.10 Testability

The crawler must be testable without hitting the internet. Approach:

- A local "evil fixtures" site served by a pytest fixture: a mini FastAPI app that emulates scenarios (login-form-cross-origin-post, sitemap-with-thousands, JS-only search, fake trust badges, redirect chain, TLS mismatch, etc.).
- Golden-file tests: run the crawler against a fixture, assert on the resulting `pages.extracted` and `evidence[*].kind` sets.
- Playwright `page.route()` for deterministic network stubbing in unit tests.
- Load tests: run 50 concurrent investigations against a local set of fixtures and measure p95.

### 4.11 Failure modes and what they map to

| Failure | Behaviour | Evidence emitted |
|---|---|---|
| DNS NXDOMAIN | Abort early, mark investigation `failed_resolution` | `infra.dns_nxdomain` |
| TLS error | Record but continue (with `ignore_https_errors` in a retry context) | `infra.tls_invalid` |
| Hard 5xx on homepage | Retry 3× exp-backoff, then fail | `crawl.homepage_unreachable` |
| `robots.txt` disallows `/` | Respect it; crawl nothing; emit and stop | `crawl.robots_full_disallow` |
| Infinite redirect loop | Stop at 10 hops | `crawl.redirect_loop` |
| Page download > 10MB | Abort that page | `crawl.page_too_large` |
| JS error on load | Keep going, record | `crawl.js_runtime_errors` |
| Known phishing kit artifact (favicon pHash in blocklist) | Short-circuit to critical | `phishing.known_kit_match` |

"No evidence found" for an analyzer emits an explicit `analyzer.no_signal` evidence row, not silence. This preserves the principle that absence of evidence is not evidence of safety.

---

## 5. Interfaces the crawler exposes to other analyzers

Other analyzers consume the crawler's output through well-defined reads, never by re-crawling.

| Analyzer | Reads from |
|---|---|
| Reverse/Template | `pages.extracted.readable_text`, `legal_pages[*].simhash`, `brand_assets[*].phash`, stored HTML in S3 |
| Phishing/Impersonation | `forms`, `outlinks`, `brand_assets`, `pages.final_url` vs `input_url`, favicon pHash |
| Reputation | `investigations.normalized_origin`, `pages.title`, contact emails/phones |
| Infra/DNS | `investigations.input_url`, redirect chain from crawler, TLS cert chain captured during render |

This separation lets you evolve, say, Template detection from "Jaccard on shingles" to "MinHash LSH over 10M corpus" without touching the crawler.

---

## 6. Build plan — milestones

**Milestone 1 — "Crawler walking skeleton" (1 week)**

- FastAPI `POST /investigations` creates a job, enqueues.
- Worker runs: planner (robots + sitemap + well-known paths), httpx tier only, stores `pages` rows and raw HTML in MinIO.
- Acceptance: run against `example.com` and a known e-commerce site; inspect `pages` rows manually.

**Milestone 2 — "Rendering + screenshots" (1 week)**

- Playwright tier, full-page + above-fold screenshots, HAR capture.
- All extractors except BrandAsset and SuspiciousUI.
- Acceptance: screenshots for each page appear in MinIO; `forms`, `outlinks` populated.

**Milestone 3 — "In-site search" (1 week)**

- Search surface detection, GET + JS-driven execution, probe runner, screenshot annotation.
- Corpus-search fallback via sqlite FTS5.
- Acceptance: against a fixture store, the probe for a banned category produces an evidence row with a labelled screenshot.

**Milestone 4 — "Evidence model + report scaffold" (1 week)**

- Evidence vocabulary frozen v0, Reporter composes a Markdown/HTML report directly from evidence rows (no LLM yet).
- Acceptance: end-to-end investigation produces a report with §1–§6 from the spec, empty where analyzers don't exist yet.

**Milestone 5+ — other analyzers** (sequenced by ROI): Infra/DNS (easy), Phishing/Impersonation (medium), Template/Reverse (medium-hard), Reputation (hard, depends on external APIs).

**Milestone Nx — LLM narrative layer:** once evidence coverage is good, use an LLM to generate the Executive Summary prose from structured evidence. The LLM sees evidence rows and must cite by id; if it hallucinates or uncited, the report composer rejects the output.

---

## 7. Risks, trade-offs, and what I am *not* doing in v1

**Doing:**
- Headless Chromium only (no Firefox/WebKit).
- Single UA; advertised and identifiable.
- Respect robots.txt fully. Yes, even if it blinds us to some sites — the cost of being seen as an abusive crawler is higher than the missed signal.
- English + a handful of languages via `langdetect`; body-text-only extraction.

**Not doing (yet):**
- **No login or form submission beyond the search box.** Credential interaction is out of scope until legal review.
- **No captcha solving.** Hit a captcha → evidence `crawl.captcha_blocked` and stop on that page.
- **No paid third-party reputation APIs in v1** (VirusTotal/URLScan/Shodan etc.). Design for them, gate behind a feature flag.
- **No LLM in the crawl loop.** LLMs are for the Reporter; putting them in crawl makes the product slow, expensive, and non-deterministic.

**Top risks I'd brief a founder on:**
1. **Evidence vocabulary drift.** If every analyzer invents its own `kind` strings, the Reporter falls apart. Mitigation: controlled vocabulary owned by one person, tests that enforce it, code review gate.
2. **Playwright flake at scale.** Headless Chromium is a beast. Mitigation: per-context isolation, hard timeouts, retries, and a test fixture suite that runs on every PR.
3. **Legal exposure from probe queries.** Typing some terms into a live site can itself look like misuse. Mitigation: probe config reviewed by counsel, rate-limited, logged, and never submitted against user-auth pages.
4. **False positives on template-match.** Many legitimate sites share Shopify/Wix templates. Mitigation: template detection must identify the *template vendor* first and compare within the vendor's baseline, not globally.
5. **Over-reliance on the LLM narrative.** The LLM is a tool to synthesise, not to judge. The risk band must be a function of evidence rows with severities — deterministic and explainable — not an LLM opinion.

---

## 8. Next step I recommend

Land **Milestone 1 + 2** as a single PR set. Everything else follows from having real, rendered `pages` rows and screenshots in MinIO. Once we have that, I can write the In-site Search module against a live fixture in a day.

I can produce the Milestone 1 scaffold in the next turn: `docker-compose.yml`, `api/` (FastAPI), `worker/` (RQ + Playwright), `db/migrations/`, and a walking-skeleton `crawl()` pipeline. Say the word.
