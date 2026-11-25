"""Amazon India Creators API adapter (OAuth 2.0 client credentials, marketplace `www.amazon.in`).

Aligned with the official Amazon Creators API v1 documentation
(`https://affiliate-program.amazon.in/creatorsapi/docs/en-us/introduction`) and
`creatorsapi-python-sdk` v1.3.0:
- Supports v3.x Login with Amazon (LwA) (`3.1`, `3.2`, `3.3`) and v2.x Cognito (`2.1`, `2.2`, `2.3`)
  token endpoints, scopes, and `Authorization` header formats.
- Supports `SearchItems` (`/catalog/v1/searchItems`) and `GetItems` (`/catalog/v1/getItems`)
  with `OffersV2` resources and `x-marketplace` header routing.
- Gracefully reports when the Amazon collaboration / API credentials are paused or when
  Associates Central exports an un-decrypted internal SIEGE envelope (`?SIEGE|...!EGEIS?`).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pcforge_scraper.config import ALL_CATEGORIES, CategorySlug, RetailerSlug, Settings, TargetsConfig, get_settings
from pcforge_scraper.fetch.api import OfficialApiClient
from pcforge_scraper.normalize.text import parse_price_paise
from pcforge_scraper.retailers.base import (
    RetailerSource,
    ScrapedListing,
    as_dict,
    as_list,
    is_combo_bundle,
    is_renewed_or_refurbished,
)

logger = logging.getLogger(__name__)

AMAZON_API_BASE = "https://creatorsapi.amazon/catalog/v1"
AMAZON_OAUTH_TOKEN_URL = "https://api.amazon.co.uk/auth/o2/token"
AMAZON_OAUTH_SCOPE = "creatorsapi::default"
AMAZON_COGNITO_SCOPE = "creatorsapi/default"
AMAZON_MIN_REQUEST_INTERVAL = 1.1  # seconds between catalog calls

AMAZON_TOKEN_ENDPOINTS_BY_VERSION: dict[str, str] = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}

AMAZON_SEARCH_RESOURCES: list[str] = [
    "images.primary.large",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "itemInfo.manufactureInfo",
    "itemInfo.productInfo",
    "itemInfo.title",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.isBuyBoxWinner",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.price",
    "parentASIN",
]


def normalize_credential_version(version: str | None) -> str:
    """Normalize an Amazon Creators API version string like `'v3.2'` to `'3.2'`."""
    if not version:
        return "3.2"
    cleaned = version.strip().lower().lstrip("v")
    return cleaned if cleaned in AMAZON_TOKEN_ENDPOINTS_BY_VERSION else "3.2"


def is_lwa_version(version: str | None) -> bool:
    """Return True if the credential version uses Login with Amazon (v3.x) rather than Cognito (v2.x)."""
    return normalize_credential_version(version).startswith("3.")


def resolve_amazon_oauth_token_url(version: str | None) -> str:
    """Return the OAuth 2.0 token endpoint for the given Creators API credential version."""
    norm = normalize_credential_version(version)
    return AMAZON_TOKEN_ENDPOINTS_BY_VERSION.get(norm, AMAZON_OAUTH_TOKEN_URL)


def resolve_amazon_oauth_scope(version: str | None) -> str:
    """Return `'creatorsapi::default'` for v3.x (LwA) or `'creatorsapi/default'` for v2.x (Cognito)."""
    return AMAZON_OAUTH_SCOPE if is_lwa_version(version) else AMAZON_COGNITO_SCOPE


def build_amazon_authorization_header(version: str | None, token: str) -> str:
    """Build the Creators API `Authorization` header per official SDK (`api_client.py`).

    - v3.x (LwA): `'Bearer <token>'` (no Version suffix)
    - v2.x (Cognito): `'Bearer <token>, Version <version>'`
    """
    norm = normalize_credential_version(version)
    if norm.startswith("3."):
        return f"Bearer {token}"
    return f"Bearer {token}, Version {norm}"


def is_siege_encrypted_secret(secret: str | None) -> bool:
    """Return True if `secret` is an un-decrypted Amazon internal SIEGE ciphertext (`?SIEGE|...!EGEIS?`)."""
    if not secret:
        return False
    s = secret.strip()
    return s.startswith("?SIEGE|") or s.endswith("!EGEIS?")


def format_creators_api_error(status_code: int, body: str, headers: Mapping[str, str] | None = None) -> str:
    """Format a structured Creators API JSON error (`type`, `reason`, `message`, `fieldList`)."""
    req_id = None
    if headers:
        for k, v in headers.items():
            if k.lower() in ("x-amzn-requestid", "x-amzn-request-id", "x-amz-request-id"):
                req_id = v
                break
    try:
        data = json.loads(body)
    except Exception:
        data = {}
    if isinstance(data, dict) and data:
        parts = [str(v) for v in (data.get("type"), data.get("reason"), data.get("message")) if v]
        fields = data.get("fieldList")
        if isinstance(fields, list) and fields:
            parts.append(f"fields={fields}")
        if parts:
            suffix = f" [request_id={req_id}]" if req_id else ""
            return f"HTTP {status_code}: {' - '.join(parts)}{suffix}"
    suffix = f" [request_id={req_id}]" if req_id else ""
    return f"HTTP {status_code}: {body[:200]}{suffix}"


class AmazonScraper(RetailerSource):
    """Official Amazon India Creators API adapter (`www.amazon.in`)."""

    # Amazon sells every category we track, peripherals included (keyboards, mice, headsets,
    # controllers). The base default covers PC parts only, which silently skipped peripherals.
    supported_categories = ALL_CATEGORIES

    slug: RetailerSlug = "amazon"
    name: str = "Amazon India"
    base_url: str = "https://www.amazon.in"

    def __init__(
        self,
        targets: TargetsConfig | None = None,
        api_client: OfficialApiClient | None = None,
        settings: Settings | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        super().__init__(targets=targets)
        self.settings = settings or get_settings()
        self.fixtures_dir = fixtures_dir
        self._external_client = api_client is not None
        self._last_request_at = float("-inf")

        client_id = (
            self.settings.amazon_creators_client_id.get_secret_value()
            if self.settings.amazon_creators_client_id
            else None
        )
        client_secret = (
            self.settings.amazon_creators_client_secret.get_secret_value()
            if self.settings.amazon_creators_client_secret
            else None
        )

        self.client = api_client or OfficialApiClient(
            service_name="amazon",
            base_url=AMAZON_API_BASE,
            default_headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "creatorsapi-python-sdk/1.3.0",
            },
            secrets_to_redact=[client_id, client_secret, self.settings.amazon_partner_tag],
        )
        # Filled by the ingest step (known Amazon listings to re-check by ASIN, per category).
        self.known_asins: dict[str, list[str]] = {}
        # Creators API calls made by this run (budget reporting).
        self.request_count = 0

    def close(self) -> None:
        """Close the underlying OfficialApiClient."""
        if not self._external_client:
            self.client.close()

    def has_credentials(self) -> bool:
        """Return True if Amazon Creators API credentials and partner tag are configured."""
        return bool(
            self.settings.amazon_creators_client_id
            and self.settings.amazon_creators_client_id.get_secret_value().strip()
            and self.settings.amazon_creators_client_secret
            and self.settings.amazon_creators_client_secret.get_secret_value().strip()
            and self.settings.amazon_partner_tag
        )

    @staticmethod
    def _display_val(node: Any) -> str | None:
        if isinstance(node, dict):
            val = node.get("displayValue") if "displayValue" in node else node.get("DisplayValue")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    @classmethod
    def parse_items_response(
        cls,
        raw_text: str,
        category: CategorySlug,
    ) -> tuple[list[ScrapedListing], int]:
        """Parse Amazon Creators API `SearchItems` / `GetItems` JSON into `(listings, skipped_count)`.

        Supports both Creators API v1 lowerCamelCase (`searchResult.items`, `offersV2`, `parentASIN`)
        and PA-API 5.0 PascalCase (`SearchResult.Items`, `Offers`, `ParentASIN`) schemas.
        """
        data = json.loads(raw_text)
        # Partial failures (e.g. ItemNotAccessible, NoResults) arrive in a 200 body.
        for err in as_list(data.get("errors") or data.get("Errors")):
            if isinstance(err, dict):
                logger.info(
                    "[amazon] API reported %s: %s",
                    err.get("code") or err.get("Code"),
                    err.get("message") or err.get("Message"),
                )
        container = (
            data.get("searchResult")
            or data.get("SearchResult")
            or data.get("itemResults")
            or data.get("itemsResult")
            or data.get("ItemsResult")
            or {}
        )
        raw_items = (
            (container.get("items") if "items" in container else container.get("Items"))
            if isinstance(container, dict)
            else []
        )
        if not isinstance(raw_items, list):
            return [], 0

        listings: list[ScrapedListing] = []
        skipped_count = 0

        for item in raw_items:
            if not isinstance(item, dict):
                continue

            asin = str(item.get("asin") or item.get("ASIN") or "").strip().upper()
            if not asin:
                continue

            detail_url = str(
                item.get("detailPageURL")
                or item.get("DetailPageURL")
                or f"https://www.amazon.in/dp/{asin}"
            ).strip()
            item_info = as_dict(item.get("itemInfo") or item.get("ItemInfo"))
            title = cls._display_val(item_info.get("title") or item_info.get("Title"))
            if not title:
                continue

            byline = as_dict(item_info.get("byLineInfo") or item_info.get("ByLineInfo"))
            brand = (
                cls._display_val(byline.get("brand") or byline.get("Brand"))
                or cls._display_val(byline.get("manufacturer") or byline.get("Manufacturer"))
            )

            mfr_info = as_dict(item_info.get("manufactureInfo") or item_info.get("ManufactureInfo"))
            part_number = cls._display_val(
                mfr_info.get("itemPartNumber") or mfr_info.get("ItemPartNumber")
            )
            model_number = cls._display_val(mfr_info.get("model") or mfr_info.get("Model"))
            raw_mpn = part_number or model_number

            prod_info = as_dict(item_info.get("productInfo") or item_info.get("ProductInfo"))
            color = cls._display_val(prod_info.get("color") or prod_info.get("Color"))
            size = cls._display_val(prod_info.get("size") or prod_info.get("Size"))

            features_node = as_dict(item_info.get("features") or item_info.get("Features"))
            features = as_list(
                features_node.get("displayValues")
                if "displayValues" in features_node
                else features_node.get("DisplayValues")
            )

            # Check offers & condition (supports offersV2 and Offers)
            offers = as_dict(
                item.get("offersV2") or item.get("OffersV2") or item.get("offers") or item.get("Offers")
            )
            offer_listings = as_list(
                offers.get("listings") if "listings" in offers else offers.get("Listings")
            )
            if not offer_listings:
                continue

            valid_new_offers: list[dict[str, Any]] = []
            non_new_condition = False
            for off in offer_listings:
                if not isinstance(off, dict):
                    continue
                cond_node = as_dict(off.get("condition") or off.get("Condition"))
                cond_val = str(
                    cond_node.get("value") or cond_node.get("Value") or "New"
                ).strip()
                if is_renewed_or_refurbished(title, condition=cond_val):
                    non_new_condition = True
                    continue
                valid_new_offers.append(off)

            if not valid_new_offers:
                if non_new_condition or is_renewed_or_refurbished(title):
                    skipped_count += 1
                continue

            # Prefer the Buy Box winner offer if flagged by OffersV2
            selected_offer = next(
                (
                    off
                    for off in valid_new_offers
                    if off.get("isBuyBoxWinner") is True or off.get("IsBuyBoxWinner") is True
                ),
                valid_new_offers[0],
            )

            if is_renewed_or_refurbished(title) or is_combo_bundle(title):
                skipped_count += 1
                continue

            price_node = as_dict(selected_offer.get("price") or selected_offer.get("Price"))
            money_node = as_dict(price_node.get("money") or price_node.get("Money"))
            raw_amount = (
                money_node.get("amount")
                or money_node.get("Amount")
                or price_node.get("amount")
                or price_node.get("Amount")
            )
            price_paise = parse_price_paise(raw_amount)
            if price_paise is None:
                continue

            saving_basis = as_dict(
                price_node.get("savingBasis")
                or price_node.get("SavingBasis")
                or selected_offer.get("savingBasis")
                or selected_offer.get("SavingBasis")
            )
            sb_money = as_dict(saving_basis.get("money") or saving_basis.get("Money"))
            raw_mrp = (
                sb_money.get("amount")
                or sb_money.get("Amount")
                or saving_basis.get("amount")
                or saving_basis.get("Amount")
            )
            mrp_paise = parse_price_paise(raw_mrp)

            avail_node = as_dict(
                selected_offer.get("availability") or selected_offer.get("Availability")
            )
            avail_type = str(
                avail_node.get("type") or avail_node.get("Type") or "Now"
            ).strip().lower()
            in_stock = avail_type in ("now", "instock", "in_stock", "available")

            images = as_dict(item.get("images") or item.get("Images"))
            primary_img = as_dict(images.get("primary") or images.get("Primary"))
            large_img = (
                as_dict(primary_img.get("large") or primary_img.get("Large"))
                or as_dict(primary_img.get("medium") or primary_img.get("Medium"))
                or as_dict(primary_img.get("small") or primary_img.get("Small"))
            )
            image_url = str(large_img.get("url") or large_img.get("URL") or "").strip() or None

            source_fields: dict[str, Any] = {
                "asin": asin,
                "parent_asin": (
                    item.get("parentASIN")
                    or item.get("parentAsin")
                    or item.get("ParentASIN")
                ),
                "part_number": part_number,
                "model": model_number,
                "color": color,
                "size": size,
                "features": features,
            }

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=detail_url,
                        external_id=asin,
                        raw_title=title,
                        raw_brand=brand,
                        raw_mpn=raw_mpn,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=in_stock,
                        image_url=image_url,
                        source_fields={k: v for k, v in source_fields.items() if v is not None},
                    )
                )
            except Exception as exc:
                logger.debug("[amazon] Skipping invalid ASIN (%s): %s", asin, exc)

        return listings, skipped_count

    def _cache_key(self) -> str:
        version = normalize_credential_version(self.settings.amazon_creators_credential_version)
        return f"amazon_creators_{version}"

    def _get_auth_headers(self) -> dict[str, str]:
        """Acquire OAuth 2.0 Bearer token and build Creators API headers."""
        assert self.settings.amazon_creators_client_id is not None
        assert self.settings.amazon_creators_client_secret is not None
        version = normalize_credential_version(self.settings.amazon_creators_credential_version)
        token_url = resolve_amazon_oauth_token_url(version)
        scope = resolve_amazon_oauth_scope(version)
        use_json = is_lwa_version(version)
        secret_val = self.settings.amazon_creators_client_secret.get_secret_value().strip()

        try:
            token = self.client.get_oauth_token(
                token_url=token_url,
                client_id=self.settings.amazon_creators_client_id.get_secret_value().strip(),
                client_secret=secret_val,
                scope=scope,
                cache_key=self._cache_key(),
                use_json=use_json,
            )
        except Exception as exc:
            if is_siege_encrypted_secret(secret_val):
                raise RuntimeError(
                    "Amazon LwA rejected the secret with 401 invalid_client because "
                    "AMAZON_CREATORS_CLIENT_SECRET contains an un-decrypted Amazon internal "
                    "SIEGE ciphertext ('?SIEGE|...!EGEIS?') exported by Associates Central "
                    "instead of the plaintext LwA secret"
                ) from exc
            raise

        return {
            "Authorization": build_amazon_authorization_header(version, token),
            "x-marketplace": self.settings.amazon_marketplace,
        }

    def _pace(self) -> None:
        """Keep catalog calls >= AMAZON_MIN_REQUEST_INTERVAL apart (the API returns 429 on bursts)."""
        wait = self._last_request_at + AMAZON_MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _post_catalog(self, path: str, payload: dict[str, Any]) -> str:
        """POST to a Creators API catalog operation with 1x automatic token refresh on 401."""
        for attempt in range(2):
            headers = self._get_auth_headers()
            self._pace()
            self.request_count += 1
            resp = self.client.post(path, json=payload, headers=headers)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 401 and attempt == 0:
                self.client.clear_oauth_token(self._cache_key())
                continue
            err_msg = format_creators_api_error(resp.status_code, resp.text, dict(resp.headers))
            raise RuntimeError(f"Amazon {path.lstrip('/')} failed ({err_msg})")
        raise RuntimeError(f"Amazon {path.lstrip('/')} failed after token refresh")

    def search_items(
        self,
        keyword: str,
        category: CategorySlug,
        item_count: int = 10,
        item_page: int = 1,
    ) -> tuple[list[ScrapedListing], int]:
        """Execute a live Creators API `searchItems` call (one page of up to 10 results; pages 1-10)."""
        payload: dict[str, Any] = {
            "keywords": keyword,
            "marketplace": self.settings.amazon_marketplace,
            "partnerTag": self.settings.amazon_partner_tag,
            "itemCount": min(max(item_count, 1), 10),
            "resources": AMAZON_SEARCH_RESOURCES,
        }
        if item_page > 1:
            payload["itemPage"] = min(item_page, 10)
        raw_text = self._post_catalog("/searchItems", payload)
        return self.parse_items_response(raw_text, category)

    def get_items(
        self,
        asins: list[str],
        category: CategorySlug,
    ) -> tuple[list[ScrapedListing], int]:
        """Execute a live Creators API `getItems` batch lookup for up to 10 ASINs."""
        payload = {
            "itemIds": asins[:10],
            "marketplace": self.settings.amazon_marketplace,
            "partnerTag": self.settings.amazon_partner_tag,
            "resources": AMAZON_SEARCH_RESOURCES,
        }
        raw_text = self._post_catalog("/getItems", payload)
        return self.parse_items_response(raw_text, category)

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch Amazon India listings for `category` honoring `quota` and `seeds`."""
        # Amazon has its own (bigger) quotas and extra brand search terms in targets.yaml.
        target_quota = max(quota or 0, self.targets.get_amazon_quota(category))
        target_seeds = self.targets.get_amazon_seeds(category) or (seeds or [])

        if self.fixtures_dir is not None:
            fpath = self.fixtures_dir / f"{category}_search.json"
            if not fpath.exists():
                self.last_skipped_counts[category] = 0
                self.last_category_notes[category] = f"Category '{category}' fixture missing"
                return []
            candidates, skipped = self.parse_items_response(
                fpath.read_text(encoding="utf-8"),
                category,
            )
            self.last_skipped_counts[category] = skipped
            selected = self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)
            skip_note = f", skipped={skipped} renewed/combo" if skipped else ""
            if len(selected) >= target_quota:
                self.last_category_notes[category] = f"OK (quota met{skip_note})"
            else:
                self.last_category_notes[category] = (
                    f"Shortfall ({len(selected)}/{target_quota}{skip_note})"
                )
            return selected

        if not self.has_credentials():
            self.last_skipped_counts[category] = 0
            self.last_category_notes[category] = (
                "Paused: Amazon collaboration paused / credentials not configured"
            )
            return []

        secret = self.settings.amazon_creators_client_secret
        if secret is not None and is_siege_encrypted_secret(secret.get_secret_value()):
            self.last_skipped_counts[category] = 0
            self.last_category_notes[category] = (
                "Invalid credentials: AMAZON_CREATORS_CLIENT_SECRET is an encrypted '?SIEGE|...' "
                "value. Re-copy (or regenerate) the plaintext credential secret from the "
                "Creators API page in Associates Central"
            )
            return []

        selected: list[ScrapedListing] = []
        total_skipped = 0
        paused = False
        start_requests = self.request_count

        # 1. Find new products: each search term, up to AMAZON_SEARCH_PAGES pages of 10 results.
        if self.settings.amazon_search:
            candidates: list[ScrapedListing] = []
            search_queries = list(target_seeds) if target_seeds else [category]
            pages = self.settings.amazon_search_pages
            for query in search_queries:
                for page in range(1, pages + 1):
                    try:
                        batch, skipped = self.search_items(query, category, item_count=10, item_page=page)
                    except Exception as exc:
                        logger.warning("[amazon] SearchItems paused/failed for %r (page %d): %s", query, page, exc)
                        self.last_category_notes[category] = self._pause_note(exc)
                        paused = True
                        break
                    candidates.extend(batch)
                    total_skipped += skipped
                    if len(batch) + skipped == 0:
                        break  # no more results for this term (pages can come back short, so only stop on empty)
                if paused or len(candidates) >= target_quota * 2:
                    break
            selected = self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)

        # 2. Refresh every Amazon listing we already know in this category, 10 ASINs per request,
        #    so their prices stay under Amazon's 24-hour display limit.
        refreshed = 0
        if not paused and self.settings.amazon_refresh_known:
            seen = {item.external_id for item in selected if item.external_id}
            asins = [a for a in self.known_asins.get(category, []) if a not in seen][: self.settings.amazon_refresh_limit]
            for i in range(0, len(asins), 10):
                try:
                    batch, skipped = self.get_items(asins[i : i + 10], category)
                except Exception as exc:
                    logger.warning("[amazon] GetItems paused/failed for %s: %s", category, exc)
                    self.last_category_notes[category] = self._pause_note(exc)
                    paused = True
                    break
                selected.extend(batch)
                refreshed += len(batch)
                total_skipped += skipped

        self.last_skipped_counts[category] = total_skipped
        used = self.request_count - start_requests
        if not paused:
            found = len(selected) - refreshed
            mode = "search" if self.settings.amazon_search else "refresh-only"
            self.last_category_notes[category] = (
                f"OK ({mode}: {found} found, {refreshed} refreshed, {used} requests, skipped={total_skipped})"
                if selected
                else f"0 listings: category '{category}' returned no matches ({used} requests)"
            )
        return selected

    @staticmethod
    def _pause_note(exc: Exception) -> str:
        if "AssociateNotEligible" in str(exc):
            # Not a code or credential problem: Amazon withholds Creators API access from
            # Associates accounts without enough recent qualifying sales, and restores it
            # by itself once they qualify again. Amazon prices turn into plain links after 24h.
            return (
                "Paused by Amazon: AssociateNotEligible (the Associates account needs recent "
                f"qualifying sales for API access; it comes back on its own). {exc}"
            )
        return f"Paused / API error: {exc}"
