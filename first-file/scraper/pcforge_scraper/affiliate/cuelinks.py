"""Cuelinks Monetization API client and per-retailer direct affiliate link builders."""

from __future__ import annotations

import os
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pcforge_scraper.config import CategorySlug, RetailerSlug, Settings, get_settings
from pcforge_scraper.fetch.api import OfficialApiClient
from pcforge_scraper.normalize.text import clean_product_url

logger = logging.getLogger(__name__)

AffiliateProviderType = Literal[
    "CUELINKS",
    "AMAZON_ASSOCIATES",
    "FLIPKART_AFFILIATE",
    "VEDANT_AFFILIATE",
    "COMPUTECH_AFFILIATE",
]

AFFILIATE_STALE_DAYS = 30

RETAILER_CAMPAIGN_QUERIES: dict[RetailerSlug, str] = {
    "amazon": "amazon",
    "flipkart": "flipkart",
    "elitehubs": "elitehubs",
    "vedant": "vedant",
    "mdcomputers": "mdcomputers",
    "computech": "computech",
}


def build_computech_affiliate_url(url: str, ref_code: str = os.environ.get("COMPUTECH_REFERRAL_CODE", "")) -> str:
    """Build a Computech Store referral link (`?ref=<ref_code>`) from any Computech URL.

    Example:
        Input URL:
            https://computechstore.in/product/intel-core-i5-14400f-lga1700-oem-desktop-processor/
            (or https://computechstore.in/)
        Output URL:
            https://computechstore.in/product/intel-core-i5-14400f-lga1700-oem-desktop-processor/?ref=<code>
            (or https://computechstore.in/?ref=<code>)
    """
    code = ref_code.strip()
    if not code:
        raise ValueError("Computech ref_code must not be empty")

    cleaned = clean_product_url(url)
    parts = urlsplit(cleaned)

    # Preserve Django trailing slash on computechstore.in product/root paths
    canonical_path = parts.path or "/"
    if parts.netloc.lower().endswith("computechstore.in") and not canonical_path.endswith("/"):
        canonical_path = f"{canonical_path}/"

    existing_params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "ref"
    ]
    existing_params.append(("ref", code))
    query = urlencode(existing_params)
    return urlunsplit((parts.scheme, parts.netloc, canonical_path, query, ""))


def build_vedant_affiliate_url(url: str, tracking_id: str) -> str:
    """Build a Vedant Computers OpenCart affiliate link (`?tracking=<tracking_id>`).

    Example:
        Input URL:
            https://www.vedantcomputers.com/pc-components/processor/amd-ryzen-5-7600x-processor
            (or https://www.vedantcomputers.com/amd-ryzen-5-7600x-processor)
        Output URL:
            https://www.vedantcomputers.com/amd-ryzen-5-7600x-processor?tracking=<tracking_id>
    """
    token = tracking_id.strip()
    if not token:
        raise ValueError("Vedant tracking_id must not be empty")

    cleaned = clean_product_url(url)
    parts = urlsplit(cleaned)

    # Canonicalize `/pc-components/<category>/<product-slug>` -> `/<product-slug>` on Vedant
    canonical_path = parts.path
    if parts.netloc.lower().endswith("vedantcomputers.com"):
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) >= 2 and segments[0].lower() == "pc-components":
            canonical_path = f"/{segments[-1]}"

    existing_params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "tracking"
    ]
    existing_params.append(("tracking", token))
    query = urlencode(existing_params)
    return urlunsplit((parts.scheme, parts.netloc, canonical_path, query, ""))


def build_amazon_affiliate_url(url: str, partner_tag: str) -> str:
    """Build an Amazon Associates affiliate link (`?tag=<partner_tag>`) from a clean URL."""
    tag = partner_tag.strip()
    if not tag:
        raise ValueError("Amazon partner_tag must not be empty")

    cleaned = clean_product_url(url)
    parts = urlsplit(cleaned)
    existing_params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "tag"
    ]
    existing_params.append(("tag", tag))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing_params), ""))


def build_flipkart_affiliate_url(url: str, affiliate_id: str) -> str:
    """Build a Flipkart Affiliate link (`?affid=<affiliate_id>`) from a clean URL."""
    affid = affiliate_id.strip()
    if not affid:
        raise ValueError("Flipkart affiliate_id must not be empty")

    cleaned = clean_product_url(url)
    parts = urlsplit(cleaned)
    existing_params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "affid"
    ]
    existing_params.append(("affid", affid))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing_params), ""))


@dataclass(frozen=True)
class AffiliateResolution:
    """Resolved affiliate link metadata ready to persist on `listing`."""

    affiliate_url: str | None
    affiliated: bool
    affiliate_provider: AffiliateProviderType | None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def build_direct_retailer_affiliate_url(
    retailer: RetailerSlug,
    clean_url: str,
    settings: Settings | None = None,
) -> AffiliateResolution | None:
    """Build a direct retailer affiliate URL if credentials/tracking ID are configured for `retailer`."""
    cfg = settings or get_settings()

    if retailer == "vedant" and cfg.vedant_computers_tracking_id is not None:
        tracking_id = cfg.vedant_computers_tracking_id.get_secret_value().strip()
        if tracking_id:
            return AffiliateResolution(
                affiliate_url=build_vedant_affiliate_url(clean_url, tracking_id),
                affiliated=True,
                affiliate_provider="VEDANT_AFFILIATE",
            )

    if retailer == "computech" and cfg.computech_referral_code is not None:
        ref_code = cfg.computech_referral_code.get_secret_value().strip()
        if ref_code:
            return AffiliateResolution(
                affiliate_url=build_computech_affiliate_url(clean_url, ref_code),
                affiliated=True,
                affiliate_provider="COMPUTECH_AFFILIATE",
            )

    if retailer == "amazon" and cfg.amazon_partner_tag:
        partner_tag = cfg.amazon_partner_tag.strip()
        if partner_tag:
            return AffiliateResolution(
                affiliate_url=build_amazon_affiliate_url(clean_url, partner_tag),
                affiliated=True,
                affiliate_provider="AMAZON_ASSOCIATES",
            )

    if retailer == "flipkart" and cfg.flipkart_affiliate_id:
        affid = cfg.flipkart_affiliate_id.strip()
        if affid:
            return AffiliateResolution(
                affiliate_url=build_flipkart_affiliate_url(clean_url, affid),
                affiliated=True,
                affiliate_provider="FLIPKART_AFFILIATE",
            )

    return None


def is_affiliate_stale(
    affiliate_url: str | None,
    affiliate_checked_at: datetime | None,
    *,
    now: datetime | None = None,
    max_age_days: int = AFFILIATE_STALE_DAYS,
) -> bool:
    """Return True if a listing's affiliate status needs checking/re-conversion.

    Re-checks when:
    - `affiliate_checked_at` is None, or
    - `affiliate_checked_at` is older than `max_age_days` (default 30 days), or
    - `affiliate_url` is empty/None AND `affiliate_checked_at` is None.
    """
    if affiliate_checked_at is None:
        return True
    ref_now = now or datetime.now(UTC)
    checked_utc = (
        affiliate_checked_at.replace(tzinfo=UTC)
        if affiliate_checked_at.tzinfo is None
        else affiliate_checked_at.astimezone(UTC)
    )
    if ref_now - checked_utc > timedelta(days=max_age_days):
        return True
    return False


@dataclass(frozen=True)
class CuelinksCampaignStatus:
    """Summary of a Cuelinks campaign lookup for a retailer."""

    query: str
    campaign_id: int | None
    name: str | None
    domain: str | None
    access_status: str | None
    payout: str | None

    @property
    def is_approved(self) -> bool:
        return (self.access_status or "").lower() in {"approved", "active"}


@dataclass(frozen=True)
class CuelinksStartupReport:
    """Result of Cuelinks startup `ping` + per-retailer campaign checks."""

    ping_ok: bool
    currency: str | None
    scopes: tuple[str, ...]
    campaigns: dict[str, CuelinksCampaignStatus]
    error: str | None = None


@dataclass(frozen=True)
class CuelinksConversionResult:
    """Parsed response from `POST /pub_api/v3/links/convert`."""

    original_url: str
    tracking_url: str | None
    affiliated: bool
    campaign_id: int | None = None
    campaign_name: str | None = None
    error: str | None = None


class CuelinksClient:
    """Client for Cuelinks Monetization API v3 (`ping`, `campaigns`, `links/convert`)."""

    def __init__(
        self,
        settings: Settings | None = None,
        api_client: OfficialApiClient | None = None,
        fixtures_dir: Path | None = None,
        cache_unaffiliated_domains: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.fixtures_dir = fixtures_dir
        self.cache_unaffiliated_domains = cache_unaffiliated_domains
        self._unaffiliated_retailers: set[RetailerSlug] = set()

        api_key = (
            self.settings.cuelinks_api_key.get_secret_value().strip()
            if self.settings.cuelinks_api_key
            else ""
        )
        self._has_api_key = bool(api_key)
        self._external_client = api_client is not None
        self.client = api_client or OfficialApiClient(
            service_name="cuelinks",
            base_url=self.settings.cuelinks_base_url,
            default_headers={
                "Authorization": f"Token {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if api_key
            else {"Accept": "application/json"},
            secrets_to_redact=[api_key] if api_key else [],
        )

    def close(self) -> None:
        if not self._external_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ping(self) -> dict[str, Any]:
        """Call `GET /ping` (or load `ping.json` in fixture mode)."""
        if self.fixtures_dir is not None:
            fpath = self.fixtures_dir / "ping.json"
            if fpath.exists():
                return dict(json.loads(fpath.read_text(encoding="utf-8")))

        if not self._has_api_key:
            raise RuntimeError("CUELINKS_API_KEY is not configured")

        resp = self.client.request("GET", "/ping")
        resp.raise_for_status()
        return dict(resp.json())

    def check_campaign(self, query: str) -> CuelinksCampaignStatus:
        """Call `GET /campaigns?q=<query>` (or load `campaigns_<query>.json` in fixture mode)."""
        payload: dict[str, Any]
        if self.fixtures_dir is not None:
            fpath = self.fixtures_dir / f"campaigns_{query}.json"
            if fpath.exists():
                payload = dict(json.loads(fpath.read_text(encoding="utf-8")))
            else:
                payload = {"data": []}
        else:
            if not self._has_api_key:
                return CuelinksCampaignStatus(
                    query=query,
                    campaign_id=None,
                    name=None,
                    domain=None,
                    access_status=None,
                    payout=None,
                )
            resp = self.client.request("GET", "/campaigns", params={"q": query})
            resp.raise_for_status()
            payload = dict(resp.json())

        rows = payload.get("data") or []
        if not rows or not isinstance(rows, list):
            return CuelinksCampaignStatus(
                query=query,
                campaign_id=None,
                name=None,
                domain=None,
                access_status=None,
                payout=None,
            )

        first = rows[0]
        for row in rows:
            domain = str(row.get("domain") or "").lower()
            countries = row.get("countries") or []
            has_india = any(
                isinstance(c, dict) and str(c.get("iso") or "").upper() == "IN"
                for c in countries
            )
            if domain.endswith(".in") or has_india:
                first = row
                break
        payout_val = first.get("payout")
        payout_curr = first.get("payout_currency") or ""
        payout_str = f"{payout_val}{payout_curr}" if payout_val is not None else None
        return CuelinksCampaignStatus(
            query=query,
            campaign_id=int(first["id"]) if first.get("id") is not None else None,
            name=first.get("name"),
            domain=first.get("domain"),
            access_status=first.get("access_status"),
            payout=payout_str,
        )

    def startup_check(
        self,
        retailers: Sequence[RetailerSlug] = (
            "amazon",
            "flipkart",
            "elitehubs",
            "vedant",
            "mdcomputers",
            "computech",
        ),
    ) -> CuelinksStartupReport:
        """Run `GET /ping` and `GET /campaigns?q=...` for each retailer; never raises."""
        try:
            ping_data = self.ping()
            publisher = ping_data.get("publisher") or {}
            api_key_meta = ping_data.get("api_key") or {}
            scopes = tuple(str(s) for s in (api_key_meta.get("scopes") or []))
            currency = publisher.get("currency")

            campaigns: dict[str, CuelinksCampaignStatus] = {}
            for r_slug in retailers:
                q = RETAILER_CAMPAIGN_QUERIES.get(r_slug, r_slug)
                try:
                    campaigns[r_slug] = self.check_campaign(q)
                except Exception as exc:
                    logger.warning("[cuelinks] Campaign check failed for %s: %s", r_slug, exc)
                    campaigns[r_slug] = CuelinksCampaignStatus(
                        query=q,
                        campaign_id=None,
                        name=None,
                        domain=None,
                        access_status=None,
                        payout=None,
                    )

            return CuelinksStartupReport(
                ping_ok=ping_data.get("status") == "ok",
                currency=currency,
                scopes=scopes,
                campaigns=campaigns,
            )
        except Exception as exc:
            logger.warning("[cuelinks] Startup check failed (continuing without Cuelinks): %s", exc)
            return CuelinksStartupReport(
                ping_ok=False,
                currency=None,
                scopes=(),
                campaigns={},
                error=str(exc),
            )

    def convert_link(
        self,
        url: str,
        *,
        product_id: int,
        category: CategorySlug | str,
        subid3: str = "pcforge-web",
        retailer: RetailerSlug | None = None,
    ) -> CuelinksConversionResult:
        """Convert a clean listing URL via `POST /links/convert`. Never raises on API errors."""
        clean_url = clean_product_url(url)
        try:
            payload: dict[str, Any]
            if self.fixtures_dir is not None and retailer is not None:
                fpath = self.fixtures_dir / f"convert_{retailer}.json"
                if fpath.exists():
                    payload = dict(json.loads(fpath.read_text(encoding="utf-8")))
                else:
                    payload = {"data": {"original_url": clean_url, "affiliated": False}}
            else:
                if not self._has_api_key:
                    return CuelinksConversionResult(
                        original_url=clean_url,
                        tracking_url=None,
                        affiliated=False,
                        error="CUELINKS_API_KEY not configured",
                    )

                body: dict[str, Any] = {
                    "url": clean_url,
                    "subid": f"p{product_id}",
                    "subid2": str(category),
                    "subid3": subid3,
                }
                if self.settings.cuelinks_channel_id is not None:
                    body["channel_id"] = self.settings.cuelinks_channel_id

                resp = self.client.request("POST", "/links/convert", json=body)
                if resp.status_code >= 400:
                    logger.warning(
                        "[cuelinks] POST /links/convert returned HTTP %d for %s",
                        resp.status_code,
                        clean_url,
                    )
                    return CuelinksConversionResult(
                        original_url=clean_url,
                        tracking_url=None,
                        affiliated=False,
                        error=f"HTTP {resp.status_code}",
                    )
                payload = dict(resp.json())

            data = payload.get("data") or {}
            affiliated = bool(data.get("affiliated", False))
            tracking_url = data.get("tracking_url") or data.get("affiliate_url")
            campaign = data.get("campaign") or {}
            return CuelinksConversionResult(
                original_url=clean_url,
                tracking_url=str(tracking_url) if tracking_url else None,
                affiliated=affiliated,
                campaign_id=int(campaign["id"]) if campaign.get("id") is not None else None,
                campaign_name=campaign.get("name"),
            )
        except Exception as exc:
            logger.warning("[cuelinks] Failed to convert link %s: %s", clean_url, exc)
            return CuelinksConversionResult(
                original_url=clean_url,
                tracking_url=None,
                affiliated=False,
                error=str(exc),
            )

    def resolve_listing_affiliate_link(
        self,
        *,
        retailer: RetailerSlug,
        clean_url: str,
        product_id: int,
        category: CategorySlug | str,
    ) -> AffiliateResolution:
        """Resolve the single best affiliate link for a `MATCHED` listing per retailer config.

        Rules (Architecture.md §7.6 & plan.md Phase 5):
        - One provider per listing; `listing.url` always stays clean.
        - Respects `settings.retailer_affiliate_preference[retailer]`:
          - `"direct_then_cuelinks"` (default for `vedant`): tries direct retailer affiliate link first
            (e.g., Vedant `?tracking=...`), else Cuelinks when `affiliated == True`, else plain URL.
          - `"cuelinks_then_direct"` (default for other stores): tries Cuelinks first when
            `affiliated == True`, else direct retailer affiliate link (`AMAZON_PARTNER_TAG`,
            `FLIPKART_AFFILIATE_ID`, `VEDANT_COMPUTERS_TRACKING_ID`), else plain URL.
        """
        preference = self.settings.retailer_affiliate_preference.get(
            retailer,
            "cuelinks_then_direct",
        )

        if preference == "direct_then_cuelinks":
            direct = build_direct_retailer_affiliate_url(retailer, clean_url, self.settings)
            if direct is not None:
                return direct

        # Try Cuelinks unless this retailer domain was already confirmed unaffiliated in this run
        if retailer not in self._unaffiliated_retailers:
            conv = self.convert_link(
                clean_url,
                product_id=product_id,
                category=category,
                retailer=retailer,
            )
            if conv.affiliated and conv.tracking_url:
                return AffiliateResolution(
                    affiliate_url=conv.tracking_url,
                    affiliated=True,
                    affiliate_provider="CUELINKS",
                )
            if (
                self.cache_unaffiliated_domains
                and conv.error is None
                and not conv.affiliated
            ):
                self._unaffiliated_retailers.add(retailer)

        # Fallback to direct retailer affiliate link if available
        direct = build_direct_retailer_affiliate_url(retailer, clean_url, self.settings)
        if direct is not None:
            return direct

        # Otherwise leave affiliate_url empty so `/out/[listingId]` redirects to the clean store URL
        return AffiliateResolution(
            affiliate_url=None,
            affiliated=False,
            affiliate_provider=None,
        )
