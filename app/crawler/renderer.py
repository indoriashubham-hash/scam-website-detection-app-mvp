"""Tier 2 rendered fetch via Playwright. One Browser per worker process, one Context
per investigation. Produces a RenderResult with everything extractors need."""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PwError,
    Page as PwPage,
    Playwright,
    TimeoutError as PwTimeout,
    async_playwright,
)

from app.config import settings

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class RenderResult:
    url: str
    final_url: str
    status: int
    mime: str | None
    title: str
    html: str
    screenshot_png: bytes
    ato_screenshot_png: bytes
    har_path: str | None
    redirect_chain: list[str]
    console_errors: list[str] = field(default_factory=list)
    cookies: list[dict] = field(default_factory=list)
    ok: bool = True
    note: str | None = None


class Renderer:
    """Owns a Playwright browser instance. One per worker process."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    @asynccontextmanager
    async def new_context(self, har_dir: str | None = None):
        assert self._browser is not None, "Renderer.start() not called"
        har_path = None
        if har_dir:
            har_path = os.path.join(har_dir, "session.har")
        ctx = await self._browser.new_context(
            user_agent=settings().crawl_user_agent,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=False,      # surface cert errors as evidence
            record_har_path=har_path,
            record_har_content="omit",
        )
        try:
            yield ctx
        finally:
            await ctx.close()

    async def render(self, context: BrowserContext, url: str) -> RenderResult:
        """Render a single URL inside the given context."""
        page: PwPage = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        final_url = url
        status = 0
        mime: str | None = None
        chain: list[str] = [url]
        ok = True
        note: str | None = None

        try:
            resp = await page.goto(
                url,
                timeout=settings().crawl_page_timeout_ms,
                wait_until="domcontentloaded",
            )
            if resp is not None:
                status = resp.status
                mime = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
                for req in resp.request.redirected_from and self._collect_chain(resp.request) or []:
                    chain.append(req)
                final_url = resp.url
            # best-effort settle; never block beyond idle ms
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=settings().crawl_nav_idle_ms
                )
            except PwTimeout:
                pass
        except PwTimeout:
            ok = False
            note = "navigation_timeout"
        except PwError as e:
            ok = False
            note = f"playwright_error:{e.message[:200]}"

        html = ""
        shot = b""
        ato = b""
        if ok:
            try:
                html = await page.content()
                shot = await page.screenshot(full_page=True, type="png")
                ato = await page.screenshot(full_page=False, type="png")
            except PwError as e:
                log.info("renderer.snapshot.err", url=url, err=str(e))

        cookies: list[dict] = []
        try:
            cookies = await context.cookies()
        except PwError:
            pass

        await page.close()
        return RenderResult(
            url=url,
            final_url=final_url,
            status=status,
            mime=mime,
            title=(await self._safe_title(page)) if ok else "",
            html=html,
            screenshot_png=shot,
            ato_screenshot_png=ato,
            har_path=None,              # HAR is per-context; pipeline picks it up on close
            redirect_chain=chain,
            console_errors=errors[:50],
            cookies=cookies,
            ok=ok,
            note=note,
        )

    @staticmethod
    def _collect_chain(_req) -> list[str]:
        # Playwright's redirect chain API is a bit clunky across versions; we treat it as
        # best-effort and prefer the response.url for the "final_url" truth.
        return []

    @staticmethod
    async def _safe_title(page: PwPage) -> str:
        try:
            return await page.title()
        except PwError:
            return ""


@asynccontextmanager
async def renderer_session():
    """Yield a started Renderer; shut it down on exit. Handy for tests."""
    r = Renderer()
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


def make_har_dir(base: Path | None = None) -> str:
    base = base or Path(tempfile.gettempdir())
    d = base / f"wri-har-{os.getpid()}-{asyncio.get_event_loop().time():.0f}"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)
