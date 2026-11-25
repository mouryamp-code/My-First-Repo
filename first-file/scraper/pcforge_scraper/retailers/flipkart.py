"""Flipkart Affiliate API adapter (`Fk-Affiliate-Id` / `Fk-Affiliate-Token`).

Fetches the category feed directory at the start of a run (Flipkart feed URLs expire after 10h)
and queries category feeds or keyword search (`/1.0/search.json`) driven by `targets.yaml` seeds.
"""

from __future__ import annotations

import json
import logging
import re
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

FLIPKART_API_BASE = "https://affiliate-api.flipkart.net/affiliate"

# Maps category slugs to candidate Flipkart Affiliate API directory feed keys
FLIPKART_FEED_KEYS: dict[CategorySlug, tuple[str, ...]] = {
    "cpu": ("computer_components", "computers", "desktops"),
    "gpu": ("computer_components", "gaming", "computers"),
    "motherboard": ("computer_components", "computers", "desktops"),
    "ram": ("computer_components", "computers", "laptop_accessories"),
    "psu": ("computer_components", "computers", "desktops"),
    "case": ("computer_components", "gaming", "computers"),
    "ssd": ("computer_storage", "computer_components", "computers"),
    "hdd": ("computer_storage", "computer_components", "computers"),
    "cooler": ("computer_components", "computers", "gaming"),
    "monitor": ("computer_peripherals", "computers", "gaming"),
    "fan": ("computer_components", "computers", "gaming"),
}

_MPN_SPEC_RE = re.compile(
    r"(?:model\s*(?:number|id|no)?|part\s*(?:number|no))\s*[:\-]\s*([A-Z0-9][A-Z0-9._/\-]{3,})",
    re.IGNORECASE,
)


class FlipkartScraper(RetailerSource):
    """Official Flipkart Affiliate API adapter (https://affiliate-api.flipkart.net/affiliate)."""

    # Flipkart sells every category we track, peripherals included (same fix as Amazon).
    supported_categories = ALL_CATEGORIES

    slug: RetailerSlug = "flipkart"
    name: str = "Flipkart"
    base_url: str = "https://www.flipkart.com"

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

        token_secret = (
            self.settings.flipkart_affiliate_token.get_secret_value()
            if self.settings.flipkart_affiliate_token
            else None
        )
        default_headers: dict[str, str] = {"Accept": "application/json"}
        if self.settings.flipkart_affiliate_id:
            default_headers["Fk-Affiliate-Id"] = self.settings.flipkart_affiliate_id
        if token_secret:
            default_headers["Fk-Affiliate-Token"] = token_secret

        self.client = api_client or OfficialApiClient(
            service_name="flipkart",
            base_url=FLIPKART_API_BASE,
            default_headers=default_headers,
            secrets_to_redact=[token_secret, self.settings.flipkart_affiliate_id],
        )
        self._directory_feeds: dict[str, str] | None = None

    def close(self) -> None:
        """Close the underlying OfficialApiClient."""
        if not self._external_client:
            self.client.close()

    def has_credentials(self) -> bool:
        """Return True if both `FLIPKART_AFFILIATE_ID` and `FLIPKART_AFFILIATE_TOKEN` are set."""
        return bool(
            self.settings.flipkart_affiliate_id
            and self.settings.flipkart_affiliate_token
            and self.settings.flipkart_affiliate_token.get_secret_value().strip()
        )

    @staticmethod
    def parse_directory_response(raw_text: str) -> dict[str, str]:
        """Parse Flipkart directory JSON (`/api/<affiliate_id>.json`) into `{feed_name: get_url}`.

        Note: Feed URLs expire (typically after 10 hours) and must never be persisted across runs.
        """
        data = json.loads(raw_text)
        listings = (
            data.get("apiGroups", {})
            .get("affiliate", {})
            .get("apiListings", {})
        )
        feeds: dict[str, str] = {}
        if not isinstance(listings, dict):
            return feeds

        for feed_name, entry in listings.items():
            if not isinstance(entry, dict):
                continue
            variants = entry.get("availableVariants", {})
            if not isinstance(variants, dict):
                continue
            variant = variants.get("v1.1.0") or variants.get("v1.0.0") or next(iter(variants.values()), None)
            if isinstance(variant, dict):
                get_url = variant.get("get")
                if isinstance(get_url, str) and get_url.strip():
                    feeds[feed_name.lower()] = get_url.strip()
        return feeds

    def fetch_category_directory(self) -> dict[str, str]:
        """Fetch fresh category feed URLs at the start of a run."""
        if self._directory_feeds is not None:
            return self._directory_feeds

        if self.fixtures_dir is not None:
            dir_path = self.fixtures_dir / "directory.json"
            if dir_path.exists():
                self._directory_feeds = self.parse_directory_response(
                    dir_path.read_text(encoding="utf-8")
                )
                return self._directory_feeds
            self._directory_feeds = {}
            return self._directory_feeds

        if not self.has_credentials():
            self._directory_feeds = {}
            return self._directory_feeds

        aff_id = self.settings.flipkart_affiliate_id
        try:
            resp = self.client.get(f"/api/{aff_id}.json")
            if resp.status_code == 200:
                self._directory_feeds = self.parse_directory_response(resp.text)
            else:
                logger.warning("[flipkart] Directory fetch returned HTTP %d", resp.status_code)
                self._directory_feeds = {}
        except Exception as exc:
            logger.warning("[flipkart] Failed to fetch category directory: %s", exc)
            self._directory_feeds = {}

        return self._directory_feeds

    @classmethod
    def parse_products_response(
        cls,
        raw_text: str,
        category: CategorySlug,
    ) -> tuple[list[ScrapedListing], int]:
        """Parse Flipkart v1.1 / v1.0 product feed or search JSON into `(listings, skipped_count)`."""
        data = json.loads(raw_text)
        raw_items = data.get("products") or data.get("productInfoList") or []
        listings: list[ScrapedListing] = []
        skipped_count = 0

        for item in raw_items:
            if not isinstance(item, dict):
                continue

            base_v1 = item.get("productBaseInfoV1")
            cat_v1 = as_dict(item.get("categorySpecificInfoV1"))
            ship_v1 = as_dict(item.get("productShippingInfoV1"))

            if isinstance(base_v1, dict):
                ext_id = str(base_v1.get("productId") or "").strip() or None
                title = str(base_v1.get("title") or "").strip()
                raw_url = str(base_v1.get("productUrl") or "").strip()
                brand = str(base_v1.get("productBrand") or "").strip() or None
                in_stock = bool(base_v1.get("inStock", True))
                attrs = as_dict(base_v1.get("attributes"))
                cat_path = str(base_v1.get("categoryPath") or "").strip() or None

                sp_obj = as_dict(base_v1.get("flipkartSpecialPrice"))
                sell_obj = as_dict(base_v1.get("flipkartSellingPrice"))
                mrp_obj = as_dict(base_v1.get("maximumRetailPrice"))

                raw_price = sp_obj.get("amount") if sp_obj.get("amount") else sell_obj.get("amount")
                raw_mrp = mrp_obj.get("amount")

                img_urls = as_dict(base_v1.get("imageUrls"))
                image_url = (
                    img_urls.get("400x400")
                    or img_urls.get("800x800")
                    or img_urls.get("200x200")
                    or img_urls.get("unknown")
                )
                discount_pct = base_v1.get("discountPercentage")
                cod_available = base_v1.get("codAvailable")
            else:
                # Fallback for v1.0 schema (`productBaseInfo.productIdentifier` & `productAttributes`)
                base_v0 = as_dict(item.get("productBaseInfo"))
                ident = as_dict(base_v0.get("productIdentifier"))
                p_attrs = as_dict(base_v0.get("productAttributes"))
                ext_id = str(ident.get("productId") or "").strip() or None
                title = str(p_attrs.get("title") or "").strip()
                raw_url = str(p_attrs.get("productUrl") or "").strip()
                brand = str(p_attrs.get("productBrand") or "").strip() or None
                in_stock = bool(p_attrs.get("inStock", True))
                attrs = {
                    "color": p_attrs.get("color"),
                    "size": p_attrs.get("size"),
                    "storage": p_attrs.get("storage"),
                    "modelNumber": p_attrs.get("modelNumber"),
                    "partNumber": p_attrs.get("partNumber"),
                }
                cat_paths = as_list(as_dict(ident.get("categoryPaths")).get("categoryPath"))
                first_group = as_list(cat_paths[0]) if cat_paths else []
                first_node = as_dict(first_group[0]) if first_group else {}
                cat_path = str(first_node.get("title") or "").strip() or None

                sell_obj = as_dict(p_attrs.get("sellingPrice"))
                mrp_obj = as_dict(p_attrs.get("maximumRetailPrice"))
                raw_price = sell_obj.get("amount")
                raw_mrp = mrp_obj.get("amount")
                img_urls = as_dict(p_attrs.get("imageUrls"))
                image_url = img_urls.get("400x400") or img_urls.get("200x200") or img_urls.get("unknown")
                discount_pct = p_attrs.get("discountPercentage")
                cod_available = p_attrs.get("codAvailable")

            if not title or not raw_url:
                continue

            # Skip renewed/used/refurbished items and multi-part combo bundles
            condition = str(attrs.get("condition") or "").strip() or None
            if is_renewed_or_refurbished(title, condition=condition, attributes=attrs) or is_combo_bundle(title):
                skipped_count += 1
                continue

            price_paise = parse_price_paise(raw_price)
            if price_paise is None:
                continue
            mrp_paise = parse_price_paise(raw_mrp)

            # Extract MPN from structured attributes or keySpecs
            key_specs = as_list(cat_v1.get("keySpecs"))
            detailed_specs = as_list(cat_v1.get("detailedSpecs"))
            raw_mpn: str | None = (
                str(attrs.get("modelNumber") or attrs.get("partNumber") or "").strip() or None
            )
            if raw_mpn is None:
                for spec_line in key_specs + detailed_specs:
                    if isinstance(spec_line, str):
                        m = _MPN_SPEC_RE.search(spec_line)
                        if m:
                            raw_mpn = m.group(1).strip()
                            break

            source_fields: dict[str, Any] = {
                "product_id": ext_id,
                "category_path": cat_path,
                "attributes": {k: v for k, v in attrs.items() if v is not None and v != ""},
                "key_specs": key_specs,
            }
            if discount_pct is not None:
                source_fields["discount_percentage"] = discount_pct
            if cod_available is not None:
                source_fields["cod_available"] = cod_available
            if ship_v1.get("sellerName"):
                source_fields["seller_name"] = ship_v1.get("sellerName")

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=raw_url,
                        external_id=ext_id,
                        raw_title=title,
                        raw_brand=brand,
                        raw_mpn=raw_mpn,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=in_stock,
                        image_url=str(image_url).strip() if image_url else None,
                        source_fields=source_fields,
                    )
                )
            except Exception as exc:
                logger.debug("[flipkart] Skipping invalid product (%s): %s", ext_id, exc)

        return listings, skipped_count

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch Flipkart listings for `category` via directory feeds or keyword search (`/1.0/search.json`)."""
        target_quota = quota if quota is not None else self.targets.get_quota(category)
        target_seeds = seeds if seeds is not None else self.targets.get_seeds(category)

        # Refresh directory at run start (feed URLs expire)
        directory_feeds = self.fetch_category_directory()
        matched_feed_key = next(
            (k for k in FLIPKART_FEED_KEYS.get(category, ()) if k in directory_feeds),
            None,
        )

        if self.fixtures_dir is not None:
            fpath = self.fixtures_dir / f"{category}_search.json"
            if not fpath.exists():
                self.last_skipped_counts[category] = 0
                self.last_category_notes[category] = f"Category '{category}' fixture missing"
                return []
            candidates, skipped = self.parse_products_response(
                fpath.read_text(encoding="utf-8"),
                category,
            )
            self.last_skipped_counts[category] = skipped
            selected = self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)
            feed_note = f"feed={matched_feed_key}" if matched_feed_key else "keyword-search"
            skip_note = f", skipped={skipped} renewed/combo" if skipped else ""
            if len(selected) >= target_quota:
                self.last_category_notes[category] = f"OK ({feed_note}{skip_note})"
            else:
                self.last_category_notes[category] = (
                    f"Shortfall ({len(selected)}/{target_quota}, {feed_note}{skip_note})"
                )
            return selected

        if not self.has_credentials():
            self.last_skipped_counts[category] = 0
            self.last_category_notes[category] = (
                "Skipped: FLIPKART_AFFILIATE_ID / FLIPKART_AFFILIATE_TOKEN not configured"
            )
            return []

        candidates = []
        total_skipped = 0

        # 1. Keyword search driven by popular-model seeds (`GET /1.0/search.json`)
        search_queries = list(target_seeds) if target_seeds else [category]
        for query in search_queries:
            try:
                resp = self.client.get(
                    "/1.0/search.json",
                    params={"query": query, "resultCount": 10},
                )
            except Exception as exc:
                logger.warning("[flipkart] Search failed for query=%r: %s", query, exc)
                continue

            if resp.status_code != 200:
                logger.warning(
                    "[flipkart] Search returned HTTP %d for query=%r",
                    resp.status_code,
                    query,
                )
                continue

            batch, skipped = self.parse_products_response(resp.text, category)
            candidates.extend(batch)
            total_skipped += skipped
            if len(candidates) >= target_quota * 2:
                break

        # 2. If keyword search returned fewer than quota and a directory feed exists, poll feed URL
        if len(candidates) < target_quota and matched_feed_key is not None:
            feed_url = directory_feeds[matched_feed_key]
            try:
                resp = self.client.get(feed_url)
                if resp.status_code == 200:
                    batch, skipped = self.parse_products_response(resp.text, category)
                    candidates.extend(batch)
                    total_skipped += skipped
            except Exception as exc:
                logger.warning("[flipkart] Category feed fetch failed (%s): %s", matched_feed_key, exc)

        self.last_skipped_counts[category] = total_skipped
        selected = self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)
        if not selected:
            self.last_category_notes[category] = (
                f"0 listings: category '{category}' not covered or returned no matches"
            )
        elif len(selected) < target_quota:
            self.last_category_notes[category] = (
                f"Shortfall ({len(selected)}/{target_quota}, skipped={total_skipped})"
            )
        else:
            self.last_category_notes[category] = f"OK (quota met, skipped={total_skipped})"
        return selected
