"""Crawl Planner — the single source of truth for what gets fetched.

Sequence:
  1. Normalize input URL; reject private/internal targets.
  2. Fetch robots.txt (Protego parser).
  3. Fetch sitemap(s) — declared by robots or the conventional locations.
  4. Seed the frontier with: homepage, well-known paths, top sitemap URLs.
  5. Offer discovered links back into the frontier with a priority score.
  6. Yield URLs in priority order until CRAWL_MAX_PAGES.

Design choices worth calling out:
- No extractor/renderer ever discovers its own URLs — they call `planner.offer(...)`
  and the planner decides. This keeps the budget enforceable.
- The planner is sync internally; IO is async via httpx. Keeps the state machine
  easy to reason about.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field

import httpx
import structlog
from protego import Protego

from app.config import settings
from app.crawler.urls import (
    AUTH_PATTERNS,
    COMMERCE_PATTERNS,
    TRUST_PATTERNS,
    WELL_KNOWN_PATHS,
    ParsedUrl,
    is_private_target,
    normalize_url,
    same_origin,
)

log = structlog.get_logger(__name__)

# The user-submitted URL is the investigation subject. It always ranks first.
# The homepage comparison slot — used when seed != root — ranks second.
SEED_PRIORITY = 10_000
HOMEPAGE_COMPARE_PRIORITY = 9_000


@dataclass(slots=True)
class FrontierItem:
    priority: int            # higher = crawl first (heapq is min-heap, we negate on push)
    url: ParsedUrl
    source: str              # "seed" | "homepage_compare" | "sitemap" | "wellknown" | "link"
    depth: int = 0


@dataclass(slots=True)
class CrawlPlan:
    seed: ParsedUrl
    homepage_compare: ParsedUrl | None = None
    robots_found: bool = False
    robots_fully_disallowed: bool = False
    robots_text: str | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)
    planned_count: int = 0


class Planner:
    def __init__(self, seed_url: str, client: httpx.AsyncClient) -> None:
        self.seed = normalize_url(seed_url)
        if is_private_target(self.seed.host):
            raise ValueError(f"refusing to crawl private/internal host: {self.seed.host}")
        self.client = client
        self.plan = CrawlPlan(seed=self.seed)
        self._seen: set[str] = set()
        self._frontier: list[tuple[int, int, FrontierItem]] = []
        self._counter = itertools.count()
        self._robots: Protego | None = None
        self._max_pages = settings().crawl_max_pages

    # ------------------------------------------------------------------
    # bootstrap
    # ------------------------------------------------------------------
    async def bootstrap(self) -> CrawlPlan:
        """Fetch robots + sitemap, seed the frontier, return the plan."""
        await self._load_robots()
        await self._load_sitemaps()
        self._seed_frontier()
        self.plan.planned_count = len(self._frontier)
        log.info(
            "planner.bootstrap",
            origin=self.seed.origin,
            planned=self.plan.planned_count,
            robots=self.plan.robots_found,
            sitemaps=len(self.plan.sitemap_urls),
        )
        return self.plan

    async def _load_robots(self) -> None:
        url = f"{self.seed.origin}/robots.txt"
        try:
            r = await self.client.get(url, timeout=10.0, follow_redirects=True)
        except httpx.HTTPError as e:
            log.info("planner.robots.err", err=str(e))
            return
        if r.status_code == 200 and r.text:
            self.plan.robots_found = True
            self.plan.robots_text = r.text[:50_000]
            self._robots = Protego.parse(r.text)
            if not self._robots.can_fetch(settings().crawl_user_agent, f"{self.seed.origin}/"):
                self.plan.robots_fully_disallowed = True

    async def _load_sitemaps(self) -> None:
        # 1) from robots
        candidates: list[str] = []
        if self._robots is not None:
            candidates.extend(self._robots.sitemaps)
        # 2) conventional locations
        candidates.extend([
            f"{self.seed.origin}/sitemap.xml",
            f"{self.seed.origin}/sitemap_index.xml",
            f"{self.seed.origin}/sitemap-index.xml",
        ])
        seen: set[str] = set()
        for sm in candidates:
            if sm in seen:
                continue
            seen.add(sm)
            urls = await self._parse_sitemap(sm, depth=0)
            if urls:
                self.plan.sitemap_urls.extend(urls)
        # de-dupe while preserving order
        self.plan.sitemap_urls = list(dict.fromkeys(self.plan.sitemap_urls))[:200]

    async def _parse_sitemap(self, url: str, depth: int) -> list[str]:
        if depth > 2:
            return []
        try:
            r = await self.client.get(url, timeout=15.0, follow_redirects=True)
        except httpx.HTTPError:
            return []
        if r.status_code != 200 or not r.text.strip().startswith("<"):
            return []
        # Very small, regex-free XML parse via lxml. We avoid ultimate-sitemap-parser to
        # skip an extra dep surface; swap in if deeper coverage is needed.
        from lxml import etree

        try:
            root = etree.fromstring(r.content)
        except etree.XMLSyntaxError:
            return []
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        out: list[str] = []
        # sitemap index
        for el in root.findall("sm:sitemap/sm:loc", ns):
            child_urls = await self._parse_sitemap(el.text.strip(), depth + 1)
            out.extend(child_urls)
        # url entries
        for el in root.findall("sm:url/sm:loc", ns):
            if el.text:
                out.append(el.text.strip())
        return out

    # ------------------------------------------------------------------
    # frontier
    # ------------------------------------------------------------------
    def _seed_frontier(self) -> None:
        # 1) SEED — user-provided URL is the investigation subject. Always first.
        self._push(self.seed, source="seed", depth=0, priority=SEED_PRIORITY)

        # 2) HOMEPAGE COMPARE — if the user pointed us at a subpage (e.g.
        # `/home.html`), also render the real root so downstream comparison can
        # spot "the subpage is a scam but the root looks clean" patterns.
        root = normalize_url(f"{self.seed.origin}/")
        if root.normalized != self.seed.normalized:
            self.plan.homepage_compare = root
            self._push(
                root, source="homepage_compare", depth=0, priority=HOMEPAGE_COMPARE_PRIORITY
            )

        # 3) well-known trust/auth/commerce paths (we still let the fetcher 404
        # them; planner only proposes).
        for p in WELL_KNOWN_PATHS:
            u = normalize_url(f"{self.seed.origin}{p}")
            self._push(u, source="wellknown", depth=1, priority=self._score(u, 1))

        # 4) sitemap URLs — only same-origin, capped.
        capped = 0
        for s in self.plan.sitemap_urls:
            if capped >= 60:
                break
            try:
                u = normalize_url(s)
            except Exception:
                continue
            if not same_origin(u, self.seed):
                continue
            self._push(u, source="sitemap", depth=1, priority=self._score(u, 1) + 5)
            capped += 1

    def _score(self, u: ParsedUrl, depth: int) -> int:
        score = 0
        if u.path in ("/", ""):
            score += 100
        if AUTH_PATTERNS.search(u.path):
            score += 80
        if TRUST_PATTERNS.search(u.path):
            score += 60
        if COMMERCE_PATTERNS.search(u.path):
            score += 50
        score += 30 if depth == 1 else 10 if depth == 2 else 0
        return score

    def _push(self, u: ParsedUrl, *, source: str, depth: int, priority: int) -> None:
        if u.normalized in self._seen:
            return
        if not same_origin(u, self.seed):
            return
        if self._robots is not None and not self._robots.can_fetch(
            settings().crawl_user_agent, u.normalized
        ):
            return
        self._seen.add(u.normalized)
        heapq.heappush(
            self._frontier,
            (-priority, next(self._counter), FrontierItem(priority, u, source, depth)),
        )

    def offer(self, url: str, *, source: str, depth: int) -> None:
        """Called by extractors (via pipeline) to propose a new URL."""
        if len(self._seen) >= self._max_pages * 3:
            # hard cap on frontier growth to prevent runaway sites
            return
        try:
            u = normalize_url(url)
        except Exception:
            return
        self._push(u, source=source, depth=depth, priority=self._score(u, depth))

    def next(self) -> FrontierItem | None:
        if not self._frontier:
            return None
        _, _, item = heapq.heappop(self._frontier)
        return item

    def iter(self) -> Iterable[FrontierItem]:
        """Yield up to CRAWL_MAX_PAGES items in priority order."""
        budget = self._max_pages
        while budget > 0:
            item = self.next()
            if item is None:
                return
            budget -= 1
            yield item


async def build_planner(seed_url: str) -> tuple[Planner, httpx.AsyncClient]:
    """Convenience: build an httpx client with sane defaults + a bootstrapped Planner."""
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    client = httpx.AsyncClient(
        headers={"User-Agent": settings().crawl_user_agent},
        limits=limits,
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        http2=True,
    )
    planner = Planner(seed_url, client)
    await planner.bootstrap()
    # silence unused asyncio warning on older lints
    _ = asyncio
    return planner, client
