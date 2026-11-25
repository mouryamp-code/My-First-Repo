"""Computech Store scraper (https://computechstore.in/) with ?ref=<code> affiliate link builder."""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from pcforge_scraper.config import CategorySlug, RetailerSlug, TargetsConfig, get_settings
from pcforge_scraper.fetch.http import HttpFetcher
from pcforge_scraper.normalize.text import parse_price_paise
from pcforge_scraper.retailers.base import (
    CategoryRoute,
    RetailerSource,
    ScrapedListing,
    extract_jsonld_products,
    is_combo_bundle,
    is_renewed_or_refurbished,
    parse_jsonld_product_fields,
)

logger = logging.getLogger(__name__)

COMPUTECH_GLOBAL_REFERRAL_URL = "https://computechstore.in/?ref=" + os.environ.get("COMPUTECH_REFERRAL_CODE", "")

COMPUTECH_CATEGORY_PATHS: dict[CategorySlug, CategoryRoute] = {
    "cpu": "/product-category/processor/",
    "gpu": "/product-category/graphics-card/",
    "motherboard": "/product-category/motherboard/",
    "ram": "/product-category/memory-ram/",
    "psu": "/product-category/power-supply/",
    "case": "/product-category/cabinet-case/",
    "ssd": "/product-category/ssd/",
    "hdd": "/product-category/hard-disk/",
    # One "cooling-system" page holds coolers, case fans and even headsets: split by title.
    "cooler": "/product-category/cooling-system/",
    "monitor": "/product-category/monitor/",
    "fan": "/product-category/cooling-system/",
    "keyboard": "/product-category/keyboards/",
    "mouse": "/product-category/mouse/",
    "headset": "/product-category/headphones/",
    "controller": "/product-category/game-controllers/",
}
MIXED_ROUTES: frozenset[str] = frozenset({"cooler", "fan"})


class ComputechScraper(RetailerSource):
    """Scraper for Computech Store (https://computechstore.in) with ?ref=<code> affiliate tagging."""

    slug: RetailerSlug = "computech"
    supported_categories = tuple(COMPUTECH_CATEGORY_PATHS)
    name: str = "Computech Store"
    base_url: str = "https://computechstore.in"
    #: robots.txt forbids every "?" URL, which is the only way its category grid paginates.
    #: Crawled anyway by the owner's choice (still rate-limited like every store).
    HONOR_ROBOTS: bool = False

    def __init__(
        self,
        targets: TargetsConfig | None = None,
        fetcher: HttpFetcher | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        super().__init__(targets=targets)
        self._external_fetcher = fetcher is not None
        self.fetcher = fetcher or HttpFetcher(retailer=self.slug, allow_browser_fallback=True, honor_robots=self.HONOR_ROBOTS)
        self.fixtures_dir = fixtures_dir
        settings = get_settings()
        self.ref_code = (
            settings.computech_referral_code.get_secret_value().strip()
            if settings.computech_referral_code is not None
            else os.environ.get("COMPUTECH_REFERRAL_CODE", "")
        ) or os.environ.get("COMPUTECH_REFERRAL_CODE", "")

    def close(self) -> None:
        if not self._external_fetcher:
            self.fetcher.close()

    @classmethod
    def parse_category_html(
        cls,
        html: str,
        category: CategorySlug,
        ref_code: str = os.environ.get("COMPUTECH_REFERRAL_CODE", ""),
    ) -> list[ScrapedListing]:
        """Parse Computech Store `#product-grid-container > div.group` product cards."""
        listings, _ = cls._parse_category_html_with_skipped(html, category, ref_code=ref_code)
        return listings

    @classmethod
    def _parse_category_html_with_skipped(
        cls,
        html: str,
        category: CategorySlug,
        ref_code: str = os.environ.get("COMPUTECH_REFERRAL_CODE", ""),
    ) -> tuple[list[ScrapedListing], int]:
        from pcforge_scraper.affiliate.cuelinks import build_computech_affiliate_url

        tree = HTMLParser(html)
        listings: list[ScrapedListing] = []
        skipped = 0

        cards = tree.css("#product-grid-container > div.group") or tree.css("div.group")
        for card in cards:
            link_node = card.css_first("a[href*='/product/']")
            h3_node = card.css_first("h3")
            if link_node is None or h3_node is None:
                continue

            raw_title = h3_node.text(strip=True)
            href = (link_node.attributes.get("href") or "").strip()
            if not raw_title or not href:
                continue

            if is_renewed_or_refurbished(raw_title) or is_combo_bundle(raw_title):
                skipped += 1
                continue

            full_url = urljoin(cls.base_url, href)

            price_paise: int | None = None
            mrp_paise: int | None = None
            for sp in card.css("span"):
                sp_text = sp.text(strip=True)
                if "₹" not in sp_text:
                    continue
                sp_cls = (sp.attributes.get("class") or "").lower()
                if "line-through" in sp_cls:
                    if mrp_paise is None:
                        mrp_paise = parse_price_paise(sp_text)
                else:
                    if price_paise is None:
                        price_paise = parse_price_paise(sp_text)

            if price_paise is None:
                skipped += 1
                continue

            card_text = card.text(separator=" ", strip=True).lower()
            in_stock = "out of stock" not in card_text and "sold out" not in card_text

            image_url: str | None = None
            img_node = card.css_first("img")
            if img_node is not None:
                raw_src = (
                    img_node.attributes.get("data-src")
                    or img_node.attributes.get("src")
                    or ""
                ).strip()
                if raw_src and not raw_src.startswith("data:"):
                    image_url = urljoin(cls.base_url, raw_src)

            affiliate_url = build_computech_affiliate_url(full_url, ref_code)

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=full_url,
                        raw_title=raw_title,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=in_stock,
                        image_url=image_url,
                        source_fields={
                            "affiliate_url": affiliate_url,
                            "global_referral_url": build_computech_affiliate_url(
                                cls.base_url,
                                ref_code,
                            ),
                        },
                    )
                )
            except Exception as exc:
                skipped += 1
                logger.debug("[computech] Skipping invalid card (%s): %s", full_url, exc)

        return listings, skipped

    @classmethod
    def parse_product_page_html(cls, html: str, ref_code: str = os.environ.get("COMPUTECH_REFERRAL_CODE", "")) -> dict[str, Any]:
        """Parse Computech Store product detail page JSON-LD (`@type: Product`) and spec tables."""
        from pcforge_scraper.affiliate.cuelinks import build_computech_affiliate_url

        details: dict[str, Any] = {}
        ld_products = extract_jsonld_products(html)
        if ld_products:
            details.update(parse_jsonld_product_fields(ld_products[0]))
            if "sku" in details and "mpn" not in details:
                details["mpn"] = details["sku"]
            if isinstance(details.get("url"), str):
                details["affiliate_url"] = build_computech_affiliate_url(
                    details["url"],
                    ref_code,
                )

        tree = HTMLParser(html)
        specs: dict[str, str] = {}
        for row in tree.css("table tr"):
            cells = row.css("th, td")
            if len(cells) == 2:
                key = cells[0].text(strip=True).rstrip(":")
                val = cells[1].text(strip=True)
                if key and val:
                    specs[key] = val
        if specs:
            details["specs"] = specs
        return details

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch Computech Store category page and return up to `quota` seed-prioritized listings."""
        target_quota = quota if quota is not None else self.targets.get_quota(category)
        target_seeds = seeds if seeds is not None else self.targets.get_seeds(category)

        if self.fixtures_dir is not None:
            cat_fixture = self.fixtures_dir / f"{category}_category.html"
            sample_fixture = self.fixtures_dir / "processor_category_sample.html"
            fpath = cat_fixture if cat_fixture.exists() else sample_fixture
            if not fpath.exists():
                return []
            candidates, skipped = self._parse_category_html_with_skipped(
                fpath.read_text(encoding="utf-8"),
                category,
                ref_code=self.ref_code,
            )
            self.last_skipped_counts[category] = skipped
            return self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)

        path = COMPUTECH_CATEGORY_PATHS[category]
        skipped_total = 0

        def parse(html: str) -> list[ScrapedListing]:
            nonlocal skipped_total
            items, skipped = self._parse_category_html_with_skipped(html, category, ref_code=self.ref_code)
            skipped_total += skipped
            return items

        # The grid paginates with ?page=N (htmx "load more"); walk it to the end.
        candidates = self.crawl_routes(
            category,
            path,
            lambda p, n: f"{self.base_url}{p}" if n == 1 else f"{self.base_url}{p}?page={n}",
            parse,
            self.fetcher.get,
            mixed=category in MIXED_ROUTES,
        )
        self.last_skipped_counts[category] = skipped_total
        return self.select_by_seeds_and_quota(candidates, quota=max(target_quota, len(candidates)), seeds=target_seeds)
