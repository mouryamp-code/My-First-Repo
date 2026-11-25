"""EliteHubs scraper (Tier A Shopify JSON + HTML fallback)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from pcforge_scraper.config import CategorySlug, RetailerSlug, TargetsConfig
from pcforge_scraper.fetch.http import HttpFetcher
from pcforge_scraper.normalize.text import parse_price_paise
from pcforge_scraper.retailers.base import (
    RetailerSource,
    ScrapedListing,
    extract_jsonld_products,
    parse_jsonld_product_fields,
)

logger = logging.getLogger(__name__)

ELITEHUBS_COLLECTION_HANDLES: dict[CategorySlug, list[str]] = {
    "cpu": ["processor", "ryzen-7000-series-processor", "intel-14th-generation-processors"],
    "gpu": ["graphic-cards", "nvidia-graphic-cards", "amd-graphic-cards"],
    "motherboard": ["gaming-motherboards", "amd-motherboard", "b650-motherboard", "b760-motherboard"],
    "ram": ["ram", "ddr5-desktop-ram", "ddr4-desktop-ram"],
    "psu": ["gold-power-supply", "bronze-power-supply", "750w-power-supply", "850w-power-supply"],
    "case": ["gaming-pc-cabinet", "atx-cabinet"],
    "ssd": ["ssd", "nvme-ssd", "sata-ssd"],
    "hdd": ["internal-hard-drive", "hard-disk"],
    "cooler": ["cpu-cooler", "liquid-cooler", "air-cooler"],
    "monitor": ["gaming-monitors", "monitors"],
    "fan": ["cabinet-fans", "case-fans"],
}

# EliteHubs sometimes puts a long slugified product title into `sku`.
# Only treat `sku` as a real MPN if it looks like an alphanumeric part number (<= 32 chars, <= 3 hyphens).
_REAL_MPN_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{3,31}$", re.IGNORECASE)


def _extract_valid_mpn(sku: str | None, handle: str | None) -> str | None:
    if not sku:
        return None
    cleaned = sku.strip()
    if not cleaned or (handle and cleaned.lower() == handle.lower()):
        return None
    if cleaned.count("-") > 3 or " " in cleaned:
        return None
    if _REAL_MPN_RE.match(cleaned) and any(ch.isdigit() for ch in cleaned):
        return cleaned.upper()
    return None


class EliteHubsScraper(RetailerSource):
    """Scraper for EliteHubs (https://elitehubs.com) using Shopify JSON with HTML fallback."""

    slug: RetailerSlug = "elitehubs"
    name: str = "EliteHubs"
    base_url: str = "https://elitehubs.com"

    def __init__(
        self,
        targets: TargetsConfig | None = None,
        fetcher: HttpFetcher | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        super().__init__(targets=targets)
        self._external_fetcher = fetcher is not None
        self.fetcher = fetcher or HttpFetcher(retailer=self.slug, allow_browser_fallback=False)
        self.fixtures_dir = fixtures_dir

    def close(self) -> None:
        if not self._external_fetcher:
            self.fetcher.close()

    @classmethod
    def parse_products_json(cls, payload: dict[str, Any] | str, category: CategorySlug) -> list[ScrapedListing]:
        """Parse Shopify `/collections/{handle}/products.json` payload into ScrapedListing items."""
        data = json.loads(payload) if isinstance(payload, str) else payload
        products = data.get("products") or []
        listings: list[ScrapedListing] = []

        for prod in products:
            handle = (prod.get("handle") or "").strip()
            title = (prod.get("title") or "").strip()
            variants = prod.get("variants") or []
            if not handle or not title or not variants:
                continue

            variant = variants[0]
            price_paise = parse_price_paise(variant.get("price"))
            if price_paise is None:
                continue

            mrp_paise = parse_price_paise(variant.get("compare_at_price"))
            in_stock = bool(variant.get("available", False))
            vendor = (prod.get("vendor") or "").strip() or None
            sku = variant.get("sku")
            raw_mpn = _extract_valid_mpn(str(sku) if sku else None, handle)

            images = prod.get("images") or []
            image_url = images[0].get("src") if images and isinstance(images[0], dict) else None

            try:
                listing = ScrapedListing(
                    retailer=cls.slug,
                    category=category,
                    url=f"{cls.base_url}/products/{handle}",
                    external_id=str(prod.get("id")) if prod.get("id") is not None else None,
                    raw_title=title,
                    raw_brand=vendor,
                    raw_mpn=raw_mpn,
                    price_paise=price_paise,
                    mrp_paise=mrp_paise,
                    in_stock=in_stock,
                    image_url=image_url,
                    source_fields={
                        "product_type": prod.get("product_type"),
                        "tags": prod.get("tags", []),
                    },
                )
                listings.append(listing)
            except Exception as exc:
                logger.debug("[elitehubs] Skipping invalid JSON product (%s): %s", handle, exc)

        return listings

    @classmethod
    def parse_collection_html(cls, html: str, category: CategorySlug) -> list[ScrapedListing]:
        """HTML fallback parser for EliteHubs collection pages when products.json is unavailable."""
        tree = HTMLParser(html)
        listings: list[ScrapedListing] = []

        card_nodes = (
            tree.css("div.product-item")
            or tree.css("div.product-card")
            or tree.css("div.grid-product")
            or tree.css("li.grid__item")
        )

        for card in card_nodes:
            ext_id = card.attributes.get("data-product-id")
            vendor = (card.attributes.get("data-vendor") or "").strip() or None
            if vendor is None:
                v_node = card.css_first(".product-vendor") or card.css_first(".card__vendor")
                if v_node:
                    vendor = v_node.text(strip=True) or None

            link = (
                card.css_first(".product-name a")
                or card.css_first(".product-title a")
                or card.css_first(".card__heading a")
                or card.css_first('a[href*="/products/"]')
            )
            if link is None:
                continue

            href = (link.attributes.get("href") or "").strip()
            raw_title = link.text(strip=True) or (link.attributes.get("title") or "").strip()
            if not href or not raw_title:
                continue

            full_url = urljoin(cls.base_url, href)

            price_node = (
                card.css_first(".current-price")
                or card.css_first(".price-item--sale")
                or card.css_first(".price-item--regular")
                or card.css_first(".product-price")
                or card.css_first(".price")
            )
            price_paise = parse_price_paise(price_node.text(strip=True) if price_node else None)
            if price_paise is None:
                continue

            mrp_node = (
                card.css_first(".compare-price")
                or card.css_first(".price-item--regular s")
                or card.css_first(".was-price")
            )
            mrp_paise = parse_price_paise(mrp_node.text(strip=True) if mrp_node else None)

            classes = (card.attributes.get("class") or "").lower()
            stock_node = card.css_first(".product-stock") or card.css_first(".badge")
            stock_txt = stock_node.text(strip=True).lower() if stock_node else ""
            in_stock = "sold-out" not in classes and "sold out" not in stock_txt and "out of stock" not in stock_txt

            img_node = card.css_first("img")
            image_url: str | None = None
            if img_node is not None:
                src = (img_node.attributes.get("data-src") or img_node.attributes.get("src") or "").strip()
                if src and not src.startswith("data:"):
                    image_url = f"https:{src}" if src.startswith("//") else urljoin(cls.base_url, src)

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=full_url,
                        external_id=ext_id.strip() if ext_id else None,
                        raw_title=raw_title,
                        raw_brand=vendor,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=in_stock,
                        image_url=image_url,
                    )
                )
            except Exception as exc:
                logger.debug("[elitehubs] Skipping invalid HTML card (%s): %s", full_url, exc)

        # Secondary fallback: extract JSON-LD Product entries if no DOM cards matched
        if not listings:
            for ld_prod in extract_jsonld_products(html):
                fields = parse_jsonld_product_fields(ld_prod)
                if fields.get("url") and fields.get("name") and fields.get("price_paise"):
                    try:
                        listings.append(
                            ScrapedListing(
                                retailer=cls.slug,
                                category=category,
                                url=urljoin(cls.base_url, fields["url"]),
                                raw_title=fields["name"],
                                raw_brand=fields.get("brand"),
                                raw_mpn=fields.get("mpn"),
                                price_paise=fields["price_paise"],
                                in_stock=fields.get("in_stock", True),
                                image_url=fields.get("image_url"),
                            )
                        )
                    except Exception:
                        continue

        return listings

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch EliteHubs collection JSON (with HTML fallback) and return up to `quota` seed-prioritized listings."""
        target_quota = quota if quota is not None else self.targets.get_quota(category)
        target_seeds = seeds if seeds is not None else self.targets.get_seeds(category)

        if self.fixtures_dir is not None:
            candidates: list[ScrapedListing] = []
            primary_json = self.fixtures_dir / f"{category}_products.json"
            if primary_json.exists():
                candidates.extend(
                    self.parse_products_json(primary_json.read_text(encoding="utf-8"), category)
                )
            if not candidates and category == "cpu":
                html_fallback = self.fixtures_dir / "processor_collection.html"
                if html_fallback.exists():
                    candidates.extend(
                        self.parse_collection_html(html_fallback.read_text(encoding="utf-8"), category)
                    )
            return self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)

        candidates = []
        for handle in ELITEHUBS_COLLECTION_HANDLES.get(category, []):
            json_url = f"{self.base_url}/collections/{handle}/products.json"
            parsed_batch: list[ScrapedListing] = []
            # Full catalog: Shopify JSON is 250 products per request, so every page of every
            # collection costs only a handful of requests and maximises cross-store overlap.
            for page in range(1, 9):
                resp = self.fetcher.get(json_url, params={"limit": 250, "page": page})
                if resp.status_code != 200:
                    break
                try:
                    page_batch = self.parse_products_json(resp.text, category)
                except Exception as exc:
                    logger.warning("[elitehubs] JSON parse failed on %s: %s", json_url, exc)
                    break
                parsed_batch.extend(page_batch)
                if len(page_batch) < 200:
                    break

            # HTML fallback if Shopify JSON endpoint failed or returned 0 items
            if not parsed_batch:
                html_url = f"{self.base_url}/collections/{handle}"
                html_resp = self.fetcher.get(html_url)
                if html_resp.status_code == 200:
                    parsed_batch = self.parse_collection_html(html_resp.text, category)

            candidates.extend(parsed_batch)

        return self.select_by_seeds_and_quota(candidates, quota=max(target_quota, len(candidates)), seeds=target_seeds)
