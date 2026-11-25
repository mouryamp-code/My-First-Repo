"""Vedant Computers scraper (OpenCart + Journal3 category grid & product JSON-LD parser)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from pcforge_scraper.config import CategorySlug, RetailerSlug, TargetsConfig
from pcforge_scraper.fetch.http import HttpFetcher
from pcforge_scraper.normalize.text import parse_price_paise
from pcforge_scraper.retailers.base import (
    CategoryRoute,
    RetailerSource,
    ScrapedListing,
    extract_jsonld_products,
    parse_jsonld_product_fields,
)

_THUMB_SIZE_RE = re.compile(r"-\d+x\d+(\.[a-z0-9]+)$", re.IGNORECASE)


def _full_size_image_url(url: str) -> str:
    """Turn an OpenCart cache thumbnail (`/image/cache/catalog/...-100x100.jpg`) into the original."""
    if "/image/cache/catalog/" not in url:
        return url
    return _THUMB_SIZE_RE.sub(r"\1", url.replace("/image/cache/catalog/", "/image/catalog/", 1))


logger = logging.getLogger(__name__)

VEDANT_CATEGORY_PATHS: dict[CategorySlug, CategoryRoute] = {
    "cpu": "/pc-components/processor",
    "gpu": "/pc-components/gpu",
    "motherboard": "/pc-components/motherboard",
    "ram": "/pc-components/memory",
    "psu": "/pc-components/smps",
    "case": "/pc-components/cabinet",
    "ssd": "/pc-components/storage/solid-state-drive",
    "hdd": "/pc-components/storage/hard-disk-drive",
    "cooler": "/pc-components/fans-and-cooling/cpu-cooler",
    "monitor": "/pc-peripherals/output-devices/monitor",
    "fan": "/pc-components/cooling-accessories/case-fan",
    "keyboard": "/pc-peripherals/input-devices/keyboard",
    "mouse": "/pc-peripherals/input-devices/mouse",
    "headset": "/pc-peripherals/output-devices/headset",
}
#: Categories whose page mixes product types (filtered by title). None on Vedant.
MIXED_ROUTES: frozenset[str] = frozenset()


def _canonicalize_vedant_product_url(url: str) -> str:
    """Canonicalize `/pc-components/<cat>/<product-slug>` -> `/<product-slug>` on Vedant."""
    parts = urlsplit(url.strip())
    canonical_path = parts.path
    if parts.netloc.lower().endswith("vedantcomputers.com"):
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) >= 2 and segments[0].lower() in ("pc-components", "peripherals"):
            canonical_path = f"/{segments[-1]}"
    return urlunsplit((parts.scheme, parts.netloc, canonical_path, parts.query, ""))


class VedantScraper(RetailerSource):
    """Scraper for Vedant Computers (https://www.vedantcomputers.com)."""

    slug: RetailerSlug = "vedant"
    supported_categories = tuple(VEDANT_CATEGORY_PATHS)
    name: str = "Vedant Computers"
    base_url: str = "https://www.vedantcomputers.com"

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
    def parse_category_html(cls, html: str, category: CategorySlug) -> list[ScrapedListing]:
        """Parse OpenCart/Journal3 `.product-layout` cards from Vedant category HTML."""
        tree = HTMLParser(html)
        listings: list[ScrapedListing] = []

        for card in tree.css("div.product-layout"):
            ext_id = card.attributes.get("data-product-id")

            name_link = card.css_first(".caption .name a")
            if name_link is None:
                continue
            raw_title = name_link.text(strip=True)
            href = (name_link.attributes.get("href") or "").strip()
            if not raw_title or not href:
                continue

            # Price (.price-new or .price-normal or .price)
            price_node = (
                card.css_first(".caption .price .price-new")
                or card.css_first(".caption .price .price-normal")
                or card.css_first(".caption .price")
            )
            price_paise = parse_price_paise(price_node.text(strip=True) if price_node else None)
            if price_paise is None:
                continue

            mrp_node = card.css_first(".caption .price .price-old")
            mrp_paise = parse_price_paise(mrp_node.text(strip=True) if mrp_node else None)

            # MPN (.stat-1 span:not(.stats-label))
            raw_mpn: str | None = None
            stat1_spans = card.css(".caption .stats .stat-1 span")
            for sp in stat1_spans:
                cls_attr = sp.attributes.get("class") or ""
                if "stats-label" not in cls_attr:
                    candidate = sp.text(strip=True)
                    if candidate:
                        raw_mpn = candidate
                        break

            # Stock status (.stat-2 span:not(.stats-label) and card class)
            card_classes = (card.attributes.get("class") or "").lower()
            in_stock = "out-of-stock" not in card_classes
            for sp in card.css(".caption .stats .stat-2 span"):
                cls_attr = sp.attributes.get("class") or ""
                if "stats-label" not in cls_attr:
                    stock_txt = sp.text(strip=True).lower()
                    if stock_txt:
                        in_stock = "in stock" in stock_txt and "out" not in stock_txt
                        break

            # Image URL
            img_node = card.css_first(".image img.img-first") or card.css_first(".image img")
            image_url: str | None = None
            if img_node is not None:
                data_src = (img_node.attributes.get("data-src") or "").strip()
                src = (img_node.attributes.get("src") or "").strip()
                if data_src and not data_src.startswith("data:"):
                    image_url = data_src
                elif src and not src.startswith("data:"):
                    image_url = src
            if image_url:
                image_url = _full_size_image_url(image_url)

            try:
                listing = ScrapedListing(
                    retailer=cls.slug,
                    category=category,
                    url=_canonicalize_vedant_product_url(href),
                    external_id=ext_id.strip() if ext_id else None,
                    raw_title=raw_title,
                    raw_mpn=raw_mpn,
                    price_paise=price_paise,
                    mrp_paise=mrp_paise,
                    in_stock=in_stock,
                    image_url=image_url,
                )
                listings.append(listing)
            except Exception as exc:
                logger.debug("[vedant] Skipping invalid card (%s): %s", href, exc)

        return listings

    @classmethod
    def parse_product_page_html(cls, html: str) -> dict[str, Any]:
        """Parse Vedant product detail page for JSON-LD `@type: Product`, `.product-stats`, and specs."""
        details: dict[str, Any] = {}

        ld_products = extract_jsonld_products(html)
        if ld_products:
            details.update(parse_jsonld_product_fields(ld_products[0]))

        tree = HTMLParser(html)

        # Extract `.product-stats` list items (Brand, Model/MPN, SKU)
        mfr_node = tree.css_first(".product-stats li.product-manufacturer a")
        if mfr_node and mfr_node.text(strip=True):
            details.setdefault("brand", mfr_node.text(strip=True))

        model_node = tree.css_first(".product-stats li.product-model span")
        if model_node and model_node.text(strip=True):
            details.setdefault("mpn", model_node.text(strip=True))

        sku_node = tree.css_first(".product-stats li.product-sku span")
        if sku_node and sku_node.text(strip=True):
            details.setdefault("sku", sku_node.text(strip=True))

        # Parse specification table rows (`#tab-specification table tr` or `.product-info table tr`)
        specs: dict[str, str] = {}
        for row in tree.css("#tab-specification table tr, table.table-bordered tr"):
            cells = row.css("td")
            if len(cells) == 2:
                key = cells[0].text(strip=True).rstrip(":")
                val = cells[1].text(strip=True)
                if key and val:
                    specs[key] = val
        if specs:
            details["specs"] = specs

        return details

    @classmethod
    def enrich_listing_from_product_html(cls, listing: ScrapedListing, html: str) -> ScrapedListing:
        """Enrich an existing category `ScrapedListing` with JSON-LD & spec details from its product page."""
        details = cls.parse_product_page_html(html)
        merged_fields = dict(listing.source_fields)
        if "specs" in details:
            merged_fields["specs"] = details["specs"]
        if details.get("sku"):
            merged_fields["sku"] = details["sku"]

        return listing.model_copy(
            update={
                "raw_brand": listing.raw_brand or details.get("brand"),
                "raw_mpn": listing.raw_mpn or details.get("mpn"),
                "image_url": listing.image_url or details.get("image_url"),
                "source_fields": merged_fields,
            }
        )

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch Vedant category page(s) and return up to `quota` seed-prioritized listings."""
        target_quota = quota if quota is not None else self.targets.get_quota(category)
        target_seeds = seeds if seeds is not None else self.targets.get_seeds(category)

        if self.fixtures_dir is not None:
            fixture_map = {
                "cpu": "processor_category.html",
                "gpu": "gpu_category.html",
                "motherboard": "motherboard_category.html",
                "ram": "memory_category.html",
                "psu": "smps_category.html",
                "case": "cabinet_category.html",
            }
            if category not in fixture_map:
                return []
            fpath = self.fixtures_dir / fixture_map[category]
            if not fpath.exists():
                return []
            candidates = self.parse_category_html(fpath.read_text(encoding="utf-8"), category)
            return self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)

        path = VEDANT_CATEGORY_PATHS[category]
        # Full catalog: every page of the category, so each product can be compared everywhere.
        candidates = self.crawl_routes(
            category,
            path,
            lambda p, n: f"{self.base_url}{p}" if n == 1 else f"{self.base_url}{p}?page={n}",
            lambda html: self.parse_category_html(html, category),
            self.fetcher.get,
            mixed=category in MIXED_ROUTES,
        )
        return self.select_by_seeds_and_quota(candidates, quota=max(target_quota, len(candidates)), seeds=target_seeds)
