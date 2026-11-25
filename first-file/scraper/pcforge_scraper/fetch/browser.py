"""Tier B Playwright browser fetcher for Cloudflare clearance and challenge pages."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from pcforge_scraper.config import Settings, get_settings
from pcforge_scraper.fetch.clearance import (
    ClearanceStore,
    DomainClearance,
    extract_domain,
    is_cloudflare_challenge,
)

logger = logging.getLogger(__name__)


@dataclass
class BrowserFetchResult:
    """Result returned by a Tier B Playwright fetch."""

    url: str
    status_code: int
    text: str
    headers: dict[str, str]
    clearance: DomainClearance


class BrowserFetcher:
    """Playwright persistent browser context (`en-IN`, `Asia/Kolkata`) that harvests cf_clearance."""

    def __init__(
        self,
        settings: Settings | None = None,
        clearance_store: ClearanceStore | None = None,
        headless: bool = True,
        challenge_wait_seconds: float = 25.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.clearance_store = clearance_store or ClearanceStore(
            self.settings.ensure_state_dir() / "clearance.json"
        )
        self.headless = headless
        self.challenge_wait_seconds = challenge_wait_seconds

    @property
    def profile_dir(self) -> Path:
        """Return persistent Chromium user data directory inside scraper/.state/."""
        path = self.settings.ensure_state_dir() / "chromium_profile"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def fetch_and_clear(self, url: str) -> BrowserFetchResult:
        """Open `url` in a persistent Chromium context, wait for Cloudflare challenge to clear, and store cookies."""
        from playwright.sync_api import sync_playwright

        domain = extract_domain(url)
        logger.info("[Tier B] Launching Playwright (headless=%s) for %s", self.headless, url)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 768},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                ],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.scrape_timeout_seconds * 1000),
                )
                status_code = response.status if response is not None else 200
                headers = dict(response.headers) if response is not None else {}
                html = page.content()

                deadline = time.monotonic() + self.challenge_wait_seconds
                while is_cloudflare_challenge(status_code, html, headers) and time.monotonic() < deadline:
                    logger.info("[Tier B] Waiting for Cloudflare challenge to clear on %s...", domain)
                    page.wait_for_timeout(1500)
                    html = page.content()
                    title = page.title().lower()
                    if "just a moment" not in title and "attention required" not in title:
                        status_code = 200
                        headers = {}
                        break

                user_agent: str = page.evaluate("() => navigator.userAgent")
                raw_cookies = context.cookies([url])
                cookie_map = {
                    str(c["name"]): str(c["value"])
                    for c in raw_cookies
                    if c.get("name") and c.get("value") is not None
                }

                clearance = self.clearance_store.save(
                    url_or_domain=domain,
                    user_agent=user_agent,
                    cookies=cookie_map,
                )
                return BrowserFetchResult(
                    url=page.url,
                    status_code=status_code,
                    text=html,
                    headers=headers,
                    clearance=clearance,
                )
            finally:
                context.close()

