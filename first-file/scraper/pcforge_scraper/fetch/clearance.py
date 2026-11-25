"""Cloudflare challenge detection and persistent clearance cookie storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from pcforge_scraper.config import get_settings

_CF_CHALLENGE_MARKERS = (
    "just a moment...",
    "window._cf_chl_opt",
    "/cdn-cgi/challenge-platform/",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
)


def extract_domain(url_or_domain: str) -> str:
    """Normalize a URL or host to a bare lowercase domain (without leading www.)."""
    raw = url_or_domain.strip().lower()
    if "://" in raw:
        host = urlsplit(raw).netloc.split(":", 1)[0]
    else:
        host = raw.split("/", 1)[0].split(":", 1)[0]
    return host.removeprefix("www.")


def is_cloudflare_challenge(
    status_code: int,
    body: str,
    headers: Mapping[str, str] | None = None,
) -> bool:
    """Return True if the HTTP response is a Cloudflare challenge or block page."""
    hdrs = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    if hdrs.get("cf-mitigated") == "challenge":
        return True

    if status_code not in (403, 429, 503):
        return False

    lower_body = body[:16384].lower()
    if any(marker in lower_body for marker in _CF_CHALLENGE_MARKERS):
        return True

    if (
        hdrs.get("server") == "cloudflare"
        and status_code in (403, 503)
        and ("<title>just a moment" in lower_body or "cloudflare" in lower_body)
    ):
        return True

    return False


class DomainClearance(BaseModel):
    """Stored clearance cookies and matching User-Agent for a single domain."""

    domain: str
    user_agent: str
    cookies: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_expired(self, max_age_minutes: int = 120) -> bool:
        """Return True if the stored clearance is older than `max_age_minutes`."""
        now = datetime.now(UTC)
        updated = (
            self.updated_at
            if self.updated_at.tzinfo is not None
            else self.updated_at.replace(tzinfo=UTC)
        )
        return (now - updated) > timedelta(minutes=max_age_minutes)


class ClearanceStore:
    """Persists Cloudflare clearance cookies (`cf_clearance` + UA) in `scraper/.state/`."""

    def __init__(self, state_file: Path | None = None) -> None:
        if state_file is None:
            state_dir = get_settings().ensure_state_dir()
            state_file = state_dir / "clearance.json"
        self.state_file = state_file

    def _read_all(self) -> dict[str, DomainClearance]:
        if not self.state_file.exists():
            return {}
        try:
            raw: dict[str, Any] = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {
                domain: DomainClearance.model_validate(entry)
                for domain, entry in raw.items()
            }
        except Exception:
            return {}

    def _write_all(self, data: dict[str, DomainClearance]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            domain: entry.model_dump(mode="json") for domain, entry in data.items()
        }
        self.state_file.write_text(
            json.dumps(serialized, indent=2), encoding="utf-8"
        )

    def get(self, url_or_domain: str, max_age_minutes: int = 120) -> DomainClearance | None:
        """Return non-expired clearance for `url_or_domain`, or None."""
        domain = extract_domain(url_or_domain)
        entry = self._read_all().get(domain)
        if entry is None or entry.is_expired(max_age_minutes=max_age_minutes):
            return None
        return entry

    def save(
        self,
        url_or_domain: str,
        user_agent: str,
        cookies: Mapping[str, str],
    ) -> DomainClearance:
        """Save clearance cookies and User-Agent for `url_or_domain`."""
        domain = extract_domain(url_or_domain)
        all_entries = self._read_all()
        entry = DomainClearance(
            domain=domain,
            user_agent=user_agent,
            cookies=dict(cookies),
            updated_at=datetime.now(UTC),
        )
        all_entries[domain] = entry
        self._write_all(all_entries)
        return entry

    def invalidate(self, url_or_domain: str) -> None:
        """Remove stored clearance for `url_or_domain` (e.g. after a 403/503)."""
        domain = extract_domain(url_or_domain)
        all_entries = self._read_all()
        if domain in all_entries:
            del all_entries[domain]
            self._write_all(all_entries)

