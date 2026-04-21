"""Crawl pipeline orchestrator.

One call, start to finish:

    async with CrawlPipeline(investigation_id, seed_url) as pipe:
        await pipe.run()

Responsibilities:
1. Drive the Planner through its frontier.
2. For each URL: static-probe → render → extract → persist page + forms + outlinks +
   evidence.
3. Let extractors suggest new URLs (same-origin) back to the Planner.
4. Upload artefacts (screenshots, HTML, HAR) to MinIO.
5. Emit a per-run ``crawl.plan`` evidence row and ``crawl.no_signal`` if nothing
   worth reporting was seen.

Concurrency: pages within an investigation are rendered one at a time in v1. This
makes ordering deterministic and keeps per-host politeness trivial. Concurrency lives
one level up: multiple investigations in parallel, each with its own context.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.extractors import DEFAULT_PIPELINE, run_pipeline
from app.crawler.extractors.base import ExtractorResult, make_context
from app.crawler.fetcher import probe
from app.crawler.planner import FrontierItem, Planner, build_planner
from app.crawler.renderer import Renderer, make_har_dir
from app.crawler.urls import TRUST_PATTERNS, ParsedUrl, normalize_url, same_origin
from app.crawler.vocabulary import Severity
from app.evidence import EvidenceEmitter
from app.models import Form, Outlink, Page
from app.storage import Storage, get_storage

log = structlog.get_logger(__name__)

# Locale-marker detectors used by the cross-page language-mismatch analyzer.
# A page that self-labels as a locale variant (``/en/about``, ``?lang=th``) is
# explicitly a translated page — we must *exclude* those from the mismatch
# check, otherwise every legitimate multilingual site would trip the signal.
_LOCALE_PATH_HINT = re.compile(r"^/[a-z]{2}(?:-[a-z]{2})?(/|$)", re.I)
_LOCALE_QUERY_HINT = re.compile(r"(?:^|&)(?:lang|locale|hl)=([a-z]{2})", re.I)


class CrawlPipeline:
    def __init__(
        self,
        *,
        session: AsyncSession,
        investigation_id: uuid.UUID,
        seed_url: str,
        renderer: Renderer,
    ) -> None:
        self.session = session
        self.investigation_id = investigation_id
        self.seed_url = seed_url
        self.renderer = renderer
        self.storage: Storage = get_storage()
        self.emitter = EvidenceEmitter(session, investigation_id, analyzer="crawl")
        self._planner: Planner | None = None
        self._http: httpx.AsyncClient | None = None
        self._pages_written = 0
        self._emitted_any_evidence = False
        # Seed-vs-homepage comparison state. Populated in _process_item and
        # consumed by _compare_seed_and_home after the crawl loop finishes.
        self._seed_sig: dict | None = None
        self._home_sig: dict | None = None
        # Off-origin dedupe: emit at most one crawl.off_origin_page_in_site
        # evidence per distinct registered domain the site silently leaves to.
        # Five in-site links all leading to the same marketing affiliate
        # shouldn't score as five separate HIGHs.
        self._off_origin_domains_seen: set[str] = set()
        # Per-page language tracking for the cross-page mismatch analyzer.
        # (analyzer itself runs post-crawl in _emit_cross_page_language.)
        self._page_langs: list[tuple[str, str]] = []  # (url, detected_lang)
        # 4xx samples collected during the crawl, for the nav-404 cluster
        # analyzer (runs post-crawl).
        self._nav_404s: list[dict] = []

    async def __aenter__(self) -> "CrawlPipeline":
        self._planner, self._http = await build_planner(self.seed_url)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        assert self._planner is not None and self._http is not None
        plan = self._planner.plan

        # Emit plan evidence (always).
        await self.emitter.emit(
            kind="crawl.plan",
            severity=Severity.INFO,
            summary=f"Planned {plan.planned_count} URLs; sitemap={'yes' if plan.sitemap_urls else 'no'}",
            confidence=1.0,
            details={
                "seed": plan.seed.normalized,
                "robots_found": plan.robots_found,
                "robots_fully_disallowed": plan.robots_fully_disallowed,
                "sitemap_count": len(plan.sitemap_urls),
                "planned_count": plan.planned_count,
            },
        )
        self._emitted_any_evidence = True

        if plan.robots_found and plan.robots_fully_disallowed:
            await self.emitter.emit(
                kind="crawl.robots_full_disallow",
                severity=Severity.MEDIUM,
                summary="robots.txt disallows our user-agent from crawling the root",
                confidence=1.0,
            )
            return

        if not plan.sitemap_urls:
            await self.emitter.emit(
                kind="crawl.sitemap_missing",
                severity=Severity.INFO,
                summary="No sitemap.xml found",
                confidence=1.0,
            )
        else:
            await self.emitter.emit(
                kind="crawl.sitemap_found",
                severity=Severity.INFO,
                summary=f"Found {len(plan.sitemap_urls)} sitemap URL(s)",
                confidence=1.0,
                details={"sitemap_sample": plan.sitemap_urls[:10]},
            )

        # HAR is per-context, so open one context per crawl.
        har_dir = make_har_dir(Path("/tmp"))
        async with self.renderer.new_context(har_dir=har_dir) as ctx:
            for item in self._planner.iter():
                await self._process_item(ctx, item)

        # Post-crawl: did the user-submitted URL materially differ from the
        # site's real homepage? This is what catches "hijacked subpage / clean
        # root" cases like wimberleymontessori.com/home.html.
        await self._compare_seed_and_home()

        # Cross-page signals that can only be evaluated once all probes/renders
        # are done. Both are "shape of the site as a whole" checks rather than
        # single-page findings.
        await self._emit_nav_404_cluster()
        await self._emit_language_mismatch_across_pages()

        if self._pages_written == 0:
            await self.emitter.emit(
                kind="crawl.homepage_unreachable",
                severity=Severity.HIGH,
                summary="Could not render any page from this site",
                confidence=0.9,
            )
        elif not self._emitted_any_evidence:
            # shouldn't happen (crawl.plan always fires) but preserve the invariant
            await self.emitter.emit(
                kind="crawl.no_signal",
                severity=Severity.INFO,
                summary="Crawl completed without notable findings",
                confidence=0.8,
            )

    # ------------------------------------------------------------------
    async def _process_item(self, ctx, item: FrontierItem) -> None:
        """Probe → render → extract → persist for one frontier item."""
        assert self._planner is not None and self._http is not None

        # 1) static probe for cheap status / mime / redirect chain.
        pr = await probe(self._http, item.url.normalized)

        # Track 4xx on trust/nav paths so we can emit the nav-404 cluster
        # signal after the crawl. Legitimate businesses have working About /
        # Contact / FAQ pages; a cluster of 4xx on these URLs is suspicious
        # regardless of whether the path was probed speculatively (wellknown)
        # or discovered via a link on the site.
        if (
            pr.status
            and 400 <= pr.status < 500
            and TRUST_PATTERNS.search(item.url.path or "")
        ):
            self._nav_404s.append({
                "url": item.url.normalized,
                "path": item.url.path,
                "status": pr.status,
                "source": item.source,
            })

        if not pr.ok and item.source == "wellknown":
            # 404 on a guessed well-known path is boring on its own; we just
            # recorded it above for the cluster analyzer. Skip the render.
            return
        if not pr.ok and item.source == "seed":
            # The investigation subject itself is unreachable — this is itself
            # a finding reviewers care about, so surface it loudly.
            await self.emitter.emit(
                kind="crawl.seed_unreachable",
                severity=Severity.HIGH,
                summary=f"User-submitted URL failed to load (probe: {pr.note or pr.status})",
                confidence=0.9,
                details={"url": item.url.normalized, "status": pr.status, "note": pr.note},
            )
            self._emitted_any_evidence = True
            return

        if pr.content_length and pr.content_length > 10 * 1024 * 1024:
            await self.emitter.emit(
                kind="crawl.page_too_large",
                severity=Severity.LOW,
                summary="Page exceeds 10MB; skipping render",
                details={"url": item.url.normalized, "bytes": pr.content_length},
            )
            return

        # 2) render with Playwright (Tier 2).
        try:
            rr = await asyncio.wait_for(
                self.renderer.render(ctx, item.url.normalized), timeout=90.0
            )
        except asyncio.TimeoutError:
            log.info("pipeline.render.timeout", url=item.url.normalized)
            if item.source == "seed":
                await self.emitter.emit(
                    kind="crawl.seed_unreachable",
                    severity=Severity.HIGH,
                    summary="User-submitted URL timed out during render",
                    confidence=0.85,
                    details={"url": item.url.normalized},
                )
                self._emitted_any_evidence = True
            return

        if not rr.ok:
            if item.source == "seed":
                await self.emitter.emit(
                    kind="crawl.seed_unreachable",
                    severity=Severity.HIGH,
                    summary=f"User-submitted URL failed to render ({rr.note})",
                    confidence=0.9,
                    details={"url": item.url.normalized, "note": rr.note},
                )
                self._emitted_any_evidence = True
            elif item.source == "homepage_compare":
                # Informational: we couldn't render the real root, so the
                # divergence comparison below will be skipped. Worth surfacing
                # so reviewers know why.
                await self.emitter.emit(
                    kind="crawl.homepage_unreachable",
                    severity=Severity.LOW,
                    summary=f"Site root failed to render ({rr.note}); divergence check skipped",
                    confidence=0.7,
                    details={"url": item.url.normalized, "note": rr.note},
                )
                self._emitted_any_evidence = True
            return

        # Redirect-chain signal — surface for the seed URL, since a chain of
        # hops from the user-submitted URL is itself interesting.
        if item.source == "seed" and len(rr.redirect_chain) > 1:
            await self.emitter.emit(
                kind="crawl.redirect_chain",
                severity=Severity.LOW,
                summary=f"Homepage redirects through {len(rr.redirect_chain)} hops",
                details={"chain": rr.redirect_chain[:10]},
            )

        # Off-origin landing detection. If an in-site URL (link, sitemap entry,
        # or well-known path) drove us to a page whose final URL lives on a
        # different registered domain, that's the classic "hidden sub-page on
        # an unrelated site" pattern. The seed itself is excluded — it IS the
        # investigation subject so its origin is authoritative, not suspicious.
        if item.source != "seed" and rr.final_url:
            try:
                final_parsed = normalize_url(rr.final_url)
                seed_ref: ParsedUrl = self._planner.seed
                if (
                    not same_origin(final_parsed, seed_ref)
                    and final_parsed.registered_domain
                    and final_parsed.registered_domain != seed_ref.registered_domain
                    and final_parsed.registered_domain not in self._off_origin_domains_seen
                ):
                    self._off_origin_domains_seen.add(final_parsed.registered_domain)
                    await self.emitter.emit(
                        kind="crawl.off_origin_page_in_site",
                        severity=Severity.HIGH,
                        summary=(
                            f"An in-site link leads to an unrelated domain "
                            f"({final_parsed.registered_domain})"
                        ),
                        confidence=0.85,
                        details={
                            "requested_url": item.url.normalized,
                            "final_url": rr.final_url,
                            "site_origin": seed_ref.origin,
                            "site_registered_domain": seed_ref.registered_domain,
                            "off_origin_host": final_parsed.host,
                            "off_origin_registered_domain": final_parsed.registered_domain,
                        },
                    )
                    self._emitted_any_evidence = True
            except Exception:  # noqa: BLE001 — URL parse failure shouldn't abort the crawl
                pass

        # 3) persist Page row (we need its id for extractor FK writes).
        page = Page(
            investigation_id=self.investigation_id,
            url=item.url.normalized,
            final_url=rr.final_url,
            http_status=rr.status,
            mime=rr.mime,
            title=rr.title[:500] if rr.title else None,
            render_mode="playwright",
            fetched_at=datetime.now(timezone.utc),
        )
        self.session.add(page)
        await self.session.flush([page])

        # 4) upload artefacts.
        shot_key = self.storage.put_bytes(
            self.investigation_id, page.id, "screenshot.png", rr.screenshot_png, "image/png"
        ).key
        ato_key = self.storage.put_bytes(
            self.investigation_id, page.id, "atf.png", rr.ato_screenshot_png, "image/png"
        ).key
        html_key = self.storage.put_bytes(
            self.investigation_id, page.id, "page.html", rr.html.encode("utf-8"), "text/html"
        ).key
        page.screenshot_key = shot_key
        page.ato_screenshot_key = ato_key
        page.html_key = html_key

        # 5) extractors.
        ectx = make_context(
            page_url=item.url.normalized,
            final_url=rr.final_url,
            html=rr.html,
            title=rr.title or "",
            status=rr.status,
            mime=rr.mime,
            cookies=rr.cookies,
            console_errors=rr.console_errors,
        )
        result: ExtractorResult = run_pipeline(ectx, DEFAULT_PIPELINE)

        # 6) write extracted + denormalized columns.
        page.lang = (result.extracted.get("lang_detected") or result.extracted.get("lang_declared"))
        page.content_hash = result.extracted.get("content_hash")
        page.simhash = result.extracted.get("simhash")
        page.word_count = result.extracted.get("readable_text_len")

        # Record the detected language for the post-crawl cross-page mismatch
        # analyzer. We only record DETECTED (not declared) because <html lang>
        # is often a copy-paste artefact and a notorious source of false signal;
        # detection is derived from the actual readable text. We also require a
        # minimum word count — too few words and detection is noise.
        detected_lang = result.extracted.get("lang_detected")
        wc = result.extracted.get("readable_text_len") or 0
        if detected_lang and wc >= 30:
            self._page_langs.append((item.url.normalized, detected_lang))
        # Mark the role of this page so the API can identify the user's URL
        # without re-deriving it (string-comparing normalized URLs is fragile).
        if item.source == "seed":
            result.extracted["is_seed"] = True
        elif item.source == "homepage_compare":
            result.extracted["is_homepage_compare"] = True
        page.extracted = _jsonable(result.extracted)

        # Stash signals for the post-crawl seed-vs-homepage comparison.
        if item.source in ("seed", "homepage_compare"):
            sig = {
                "url": item.url.normalized,
                "page_id": page.id,
                "title": (rr.title or "")[:300],
                "simhash": result.extracted.get("simhash"),
                "word_count": result.extracted.get("readable_text_len") or 0,
                "form_count": len(result.forms),
                "login_form": any(f.get("is_login") for f in result.forms),
                "screenshot_key": page.ato_screenshot_key,
            }
            if item.source == "seed":
                self._seed_sig = sig
            else:
                self._home_sig = sig

        # forms
        for f in result.forms:
            self.session.add(
                Form(
                    page_id=page.id,
                    action=f.get("action"),
                    method=f.get("method"),
                    fields=f.get("fields") or [],
                    is_login=bool(f.get("is_login")),
                    is_payment=bool(f.get("is_payment")),
                    posts_cross_origin=bool(f.get("posts_cross_origin")),
                )
            )

        # outlinks + offer same-origin links back to the planner
        seed_origin: ParsedUrl = self._planner.seed
        for link in result.links:
            try:
                u = normalize_url(link.href)
            except Exception:
                continue
            same = same_origin(u, seed_origin)
            self.session.add(
                Outlink(
                    page_id=page.id,
                    href=u.normalized,
                    rel=link.rel,
                    anchor_text=link.anchor_text,
                    same_origin=same,
                    registered_domain=u.registered_domain,
                )
            )
            if same:
                self._planner.offer(u.normalized, source="link", depth=item.depth + 1)

        # extractor-proposed evidence
        for e in result.evidence:
            await self.emitter.emit(
                kind=e.kind,
                severity=e.severity,
                summary=e.summary,
                confidence=e.confidence,
                details=e.details,
                page_id=page.id,
                screenshot_key=page.ato_screenshot_key,
            )
            self._emitted_any_evidence = True

        # JS runtime errors → weak evidence (cap one per page)
        if rr.console_errors:
            await self.emitter.emit(
                kind="crawl.js_runtime_errors",
                severity=Severity.INFO,
                summary=f"{len(rr.console_errors)} console error(s) while rendering",
                details={"sample": rr.console_errors[:5]},
                page_id=page.id,
            )

        await self.session.flush()
        self._pages_written += 1


    # ------------------------------------------------------------------
    async def _compare_seed_and_home(self) -> None:
        """Compare the user-submitted URL (seed) to the site's real homepage.

        If they're materially different, emit ``crawl.seed_vs_homepage_divergence``
        — this is the signal that catches "hijacked subpage / clean root" cases
        like ``wimberleymontessori.com/home.html``.

        Materiality is deliberately conservative: we require either a large
        simhash distance OR a clear structural mismatch (login form on one but
        not the other, very different word counts, different titles). A single
        weak signal is not enough because legitimate sites often have distinct
        landing pages per product.
        """
        seed = self._seed_sig
        home = self._home_sig
        if seed is None or home is None:
            return  # user pointed at the root, or one side didn't render

        diffs: list[str] = []
        details: dict = {
            "seed_url": seed["url"],
            "homepage_url": home["url"],
            "seed_title": seed["title"],
            "homepage_title": home["title"],
            "seed_word_count": seed["word_count"],
            "homepage_word_count": home["word_count"],
        }

        # 1) Simhash hamming distance — 0..64, higher = more different.
        # Simhashes are stored as signed int64 (Postgres BIGINT constraint), so
        # mask back to the unsigned 64-bit bit-pattern before XOR — otherwise
        # popcount on a negative int includes the infinite leading sign bits
        # and we'd get garbage distances.
        _MASK64 = (1 << 64) - 1
        hamming: int | None = None
        if seed["simhash"] is not None and home["simhash"] is not None:
            a = int(seed["simhash"]) & _MASK64
            b = int(home["simhash"]) & _MASK64
            hamming = bin(a ^ b).count("1")
            details["simhash_distance"] = hamming
            if hamming >= 24:                       # ~38% bits differ → distinct content
                diffs.append(f"body text differs (simhash distance={hamming}/64)")

        # 2) Title mismatch (case-insensitive, trimmed).
        if seed["title"].strip().lower() != home["title"].strip().lower():
            diffs.append("titles differ")

        # 3) Login-form presence asymmetry. Strong signal: a page that asks for
        # credentials but whose site root doesn't is a classic phishing shape.
        if seed["login_form"] and not home["login_form"]:
            diffs.append("login form on seed but not on homepage")
            details["login_form_on_seed_only"] = True
        elif home["login_form"] and not seed["login_form"]:
            diffs.append("login form on homepage but not on seed")

        # 4) Gross word-count disparity (>5x). Cheap landing-page vs full site.
        sw, hw = seed["word_count"], home["word_count"]
        if sw and hw and (max(sw, hw) / max(min(sw, hw), 1)) >= 5:
            diffs.append(f"word-count ratio {sw}:{hw}")

        # Decision: at least one strong diff (simhash OR login asymmetry) + one
        # corroborating diff → divergence. Otherwise "similar" (informational).
        strong = (
            (hamming is not None and hamming >= 24)
            or seed["login_form"] != home["login_form"]
        )
        if strong and diffs:
            await self.emitter.emit(
                kind="crawl.seed_vs_homepage_divergence",
                severity=Severity.HIGH,
                summary=(
                    "User-submitted URL differs materially from the site's homepage: "
                    + "; ".join(diffs)
                ),
                confidence=0.85,
                details=details,
                page_id=seed["page_id"],
                screenshot_key=seed["screenshot_key"],
            )
            self._emitted_any_evidence = True
        else:
            await self.emitter.emit(
                kind="crawl.seed_vs_homepage_similar",
                severity=Severity.INFO,
                summary="User-submitted URL and site homepage appear consistent",
                confidence=0.7,
                details=details,
                page_id=seed["page_id"],
            )


    # ------------------------------------------------------------------
    async def _emit_nav_404_cluster(self) -> None:
        """Emit ``crawl.nav_404_cluster`` if several trust pages returned 4xx.

        Legitimate businesses have working About / Contact / FAQ / Terms /
        Privacy pages — reviewers, regulators, and customers look for them.
        A cluster of 4xx responses on these standard paths is a real-business
        smell regardless of whether the path was probed speculatively (the
        well-known list) or discovered via an in-site link.

        We dedupe by trust-keyword *family* so that ``/about`` and
        ``/about-us`` both failing count as a single "about" family. Without
        this, pages that link `/about`, `/about-us`, and `/about/team` would
        all trip one family three times and produce inflated confidence.

        We require at least 2 distinct families before emitting — a single
        404 on `/about` is common enough (typos, renames) that it would be
        noisy. Two or more is the point where "probably not a real business"
        becomes the cheaper explanation.
        """
        if len(self._nav_404s) < 2:
            return

        # Group by trust-keyword so distinct topics count, not distinct URLs.
        # The TRUST_PATTERNS regex's first capture is the keyword itself;
        # normalize trailing 's' (returns/return, refunds/refund) to collapse.
        families: dict[str, list[dict]] = {}
        for item in self._nav_404s:
            m = TRUST_PATTERNS.search(item.get("path") or "")
            if not m:
                continue
            family = m.group(1).lower().rstrip("s")
            families.setdefault(family, []).append(item)

        if len(families) < 2:
            return

        await self.emitter.emit(
            kind="crawl.nav_404_cluster",
            severity=Severity.MEDIUM,
            summary=(
                f"{len(families)} standard trust pages returned 4xx "
                f"({', '.join(sorted(families))})"
            ),
            confidence=0.85,
            details={
                "families": sorted(families),
                "samples": self._nav_404s[:8],
            },
        )
        self._emitted_any_evidence = True

    # ------------------------------------------------------------------
    async def _emit_language_mismatch_across_pages(self) -> None:
        """Emit ``crawl.language_mismatch_across_pages`` when the site's pages
        use distinct languages without labelling themselves as locale variants.

        Real multilingual sites label their locales: ``/en/about``,
        ``/th/contact``, ``?lang=en``, ``?locale=th``, or distinct
        hreflang-mapped routes. A site where the homepage detects as English
        and inner pages detect as Thai *without any such markers* is classic
        "content stitched onto an unrelated domain" — the exact case that
        wimberleymontessori.com presented (English nursery school branding on
        top of Thai casino content).

        Detection rules:
        1. Only pages with >=30 readable words are considered (per
           ``_process_item``), so thin pages don't pollute the result.
        2. Pages whose URL is *itself* a locale variant (``/en/*`` path or
           ``?lang=xx`` query) are filtered out — they're explicitly labeled.
        3. If the remaining pages span 2+ distinct base languages (en, th,
           ru, …), we emit. Regional variants collapse to the base (en-US →
           en, zh-Hans → zh) since they'd rarely be the real mismatch.
        """
        if len(self._page_langs) < 2:
            return

        def _base(code: str) -> str:
            return (code or "").split("-")[0].lower()

        def _has_locale_hint(url: str) -> bool:
            try:
                p = normalize_url(url)
            except Exception:  # noqa: BLE001 — parse failure shouldn't crash the run
                return False
            if _LOCALE_PATH_HINT.match(p.path or ""):
                return True
            if _LOCALE_QUERY_HINT.search(p.query or ""):
                return True
            return False

        unhinted = [
            (u, _base(l)) for u, l in self._page_langs
            if l and not _has_locale_hint(u)
        ]
        distinct = sorted({l for _, l in unhinted if l})
        if len(distinct) < 2:
            return

        await self.emitter.emit(
            kind="crawl.language_mismatch_across_pages",
            severity=Severity.MEDIUM,
            summary=(
                f"Pages on this site use different languages ({', '.join(distinct)}) "
                f"without locale markers in their URLs"
            ),
            confidence=0.75,
            details={
                "languages": distinct,
                "samples": [{"url": u, "lang": l} for u, l in unhinted[:8]],
            },
        )
        self._emitted_any_evidence = True


def _jsonable(d: dict) -> dict:
    """Shallow cast-to-JSON-safe for simhash ints and the like."""
    out = {}
    for k, v in d.items():
        if isinstance(v, (bytes, bytearray)):
            continue
        out[k] = v
    return out
