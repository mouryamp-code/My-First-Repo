"""Tier A curl_cffi HTTP fetcher with Chrome TLS impersonation, cookie reuse, and Tier B fallback."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Self
from urllib.parse import urlencode, urlsplit

from curl_cffi import requests as curl_requests

from pcforge_scraper.config import Settings, get_settings
from pcforge_scraper.fetch.browser import BrowserFetcher
from pcforge_scraper.fetch.clearance import (
    ClearanceStore,
    is_cloudflare_challenge,
)
from pcforge_scraper.fetch.limiter import RetailerRateLimiter

logger = logging.getLogger(__name__)


@dataclass
class HttpResponse:
    """Normalized HTTP response returned by HttpFetcher."""

    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    tier_used: Literal["A", "B"] = "A"

    def json(self) -> Any:
        """Parse response body as JSON."""
        return json.loads(self.text)


class HttpFetcher:
    """Tier A -> Tier B escalating HTTP fetcher for a single retailer."""

    def __init__(
        self,
        retailer: str,
        settings: Settings | None = None,
        limiter: RetailerRateLimiter | None = None,
        clearance_store: ClearanceStore | None = None,
        browser_fetcher: BrowserFetcher | None = None,
        allow_browser_fallback: bool = True,
        honor_robots: bool = True,
        impersonate: str = "chrome",
    ) -> None:
        self.retailer = retailer
        self.settings = settings or get_settings()
        self.limiter = limiter or RetailerRateLimiter(
            retailer=retailer,
            min_delay=self.settings.scrape_min_delay,
            max_delay=self.settings.scrape_max_delay,
            max_consecutive_failures=self.settings.scrape_max_consecutive_failures,
        )
        self.clearance_store = clearance_store or ClearanceStore(
            self.settings.ensure_state_dir() / "clearance.json"
        )
        self.browser_fetcher = browser_fetcher or BrowserFetcher(
            settings=self.settings,
            clearance_store=self.clearance_store,
        )
        self.allow_browser_fallback = allow_browser_fallback
        self.honor_robots = honor_robots
        self.impersonate = impersonate
        self._session = curl_requests.Session(impersonate=impersonate)
        self._robots_loaded_for: set[str] = set()

    def close(self) -> None:
        """Close the underlying curl_cffi session."""
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _apply_clearance(self, url: str) -> dict[str, str]:
        """Return request headers and apply any stored clearance cookies for `url`."""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "User-Agent": self.settings.user_agent,
        }
        clearance = self.clearance_store.get(url)
        if clearance is not None:
            headers["User-Agent"] = clearance.user_agent
            for k, v in clearance.cookies.items():
                self._session.cookies.set(k, v, domain=clearance.domain)
        return headers

    def _ensure_robots_loaded(self, url: str) -> None:
        """Fetch and cache robots.txt for the target origin once per session."""
        if not self.honor_robots or self.limiter.robots_policy is not None:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots_loaded_for:
            return
        self._robots_loaded_for.add(origin)

        robots_url = f"{origin}/robots.txt"
        try:
            headers = self._apply_clearance(robots_url)
            resp = self._session.get(
                robots_url,
                headers=headers,
                timeout=self.settings.scrape_timeout_seconds,
            )
            if resp.status_code == 200 and not is_cloudflare_challenge(
                resp.status_code,
                resp.text,
                {str(k): str(v) for k, v in resp.headers.items() if v is not None},
            ):
                self.limiter.set_robots_txt(resp.text, user_agent="*")
        except Exception as exc:
            logger.debug("[%s] Could not load robots.txt (%s): %s", self.retailer, robots_url, exc)

    def get(
        self,
        url: str,
        params: Mapping[str, str | int] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        max_attempts: int = 3,
    ) -> HttpResponse:
        """Fetch `url` using Tier A (`curl_cffi`), escalating to Tier B (`Playwright`) on Cloudflare challenge."""
        self._ensure_robots_loaded(url)
        # Check the URL exactly as it will be requested, query string included.
        full_url = f"{url}{'&' if '?' in url else '?'}{urlencode(dict(params))}" if params else url
        self.limiter.check_robots(full_url)

        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self.limiter.wait_before_request()
            headers = self._apply_clearance(url)
            if extra_headers:
                headers.update(extra_headers)

            try:
                resp = self._session.get(
                    url,
                    params=dict(params) if params else None,
                    headers=headers,
                    timeout=self.settings.scrape_timeout_seconds,
                )
                resp_headers = {str(k): str(v) for k, v in resp.headers.items()}
                body = resp.text
                status = int(resp.status_code)
            except Exception as exc:
                last_exc = exc
                self.limiter.record_failure(status_code=503, attempt=attempt)
                continue

            if is_cloudflare_challenge(status, body, resp_headers):
                logger.info(
                    "[%s] Cloudflare challenge detected on %s (HTTP %d, attempt %d)",
                    self.retailer,
                    url,
                    status,
                    attempt,
                )
                self.clearance_store.invalidate(url)
                if self.allow_browser_fallback:
                    browser_res = self.browser_fetcher.fetch_and_clear(url)
                    if not is_cloudflare_challenge(
                        browser_res.status_code, browser_res.text, browser_res.headers
                    ):
                        self.limiter.record_success()
                        return HttpResponse(
                            url=browser_res.url,
                            status_code=browser_res.status_code,
                            text=browser_res.text,
                            headers=browser_res.headers,
                            tier_used="B",
                        )
                self.limiter.record_failure(status_code=status, attempt=attempt)
                continue

            if status in (429, 502, 503, 504):
                self.limiter.record_failure(status_code=status, attempt=attempt)
                continue

            if status >= 400:
                self.limiter.record_failure(status_code=status, attempt=attempt)
                return HttpResponse(
                    url=str(resp.url),
                    status_code=status,
                    text=body,
                    headers=resp_headers,
                    tier_used="A",
                )

            self.limiter.record_success()
            return HttpResponse(
                url=str(resp.url),
                status_code=status,
                text=body,
                headers=resp_headers,
                tier_used="A",
            )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"[{self.retailer}] Exhausted {max_attempts} attempts fetching {url}")

