"""MD Computers scraper (Cloudflare Tier B clearance -> Tier A OpenCart/RetrinaPro parser)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

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

logger = logging.getLogger(__name__)

# The short paths (/graphics-card) redirect to /catalog/... and the redirect drops ?page=N, so
# use the final /catalog/ URLs or pagination silently returns page 1 every time.
MD_CATEGORY_PATHS: dict[CategorySlug, CategoryRoute] = {
    "cpu": "/catalog/processor",
    "gpu": "/catalog/graphics-card",
    "motherboard": "/catalog/motherboard",
    "ram": "/catalog/ram",
    "psu": "/catalog/smps",
    "case": "/catalog/cabinet",
    "ssd": "/catalog/ssd-drive",
    "hdd": "/catalog/internal-hdd",
    # The parent /catalog/cpu-cooler also lists thermal paste and accessories.
    "cooler": ("/catalog/cpu-cooler/cpu-air-cooler", "/catalog/cpu-cooler/cpu-liquid-cooler"),
    "monitor": "/catalog/monitor",
    # No case-fan category: fans sit among other cooling accessories.
    "fan": "/catalog/cpu-cooler/cooling-accessories",
    "keyboard": "/catalog/gaming-keyboard",
    "mouse": "/catalog/gaming-mouse",
    "headset": "/catalog/gaming-headset",
    "controller": "/catalog/gamepad",
}
#: Categories whose page mixes product types (filtered by title).
MIXED_ROUTES: frozenset[str] = frozenset({"fan"})

_CART_ADD_RE = re.compile(r"cart\.add\(\s*['\"](\d+)['\"]")
_IMG_MPN_RE = re.compile(r"/image/catalog/[^/]+/[^/]+/([a-z0-9][a-z0-9._-]{3,})/", re.IGNORECASE)
# Frozen: listings are unique on (retailer, url), so changing which suffixes get stripped would
# re-create every existing MD listing under a new URL. These are the suffixes stripped so far.
_CATEGORY_SUFFIXES = (
    "/processor", "/graphics-card", "/motherboards", "/memory", "/smps", "/cabinet",
    "/ssd-drive", "/internal-hdd", "/cooling-system", "/monitors", "/case-cooling-fan",
)


def _canonicalize_md_product_url(url: str) -> str:
    """Strip trailing category path suffix (e.g. `/processor`) from `/product/<slug>/<cat>` URLs."""
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    if path.startswith("/product/"):
        for suffix in _CATEGORY_SUFFIXES:
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


class MDComputersScraper(RetailerSource):
    """Scraper for MD Computers (https://mdcomputers.in) with Playwright clearance fallback."""

    slug: RetailerSlug = "mdcomputers"
    supported_categories = tuple(MD_CATEGORY_PATHS)
    name: str = "MD Computers"
    base_url: str = "https://mdcomputers.in"

    def __init__(
        self,
        targets: TargetsConfig | None = None,
        fetcher: HttpFetcher | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        super().__init__(targets=targets)
        self._external_fetcher = fetcher is not None
        self.fetcher = fetcher or HttpFetcher(retailer=self.slug, allow_browser_fallback=True)
        self.fixtures_dir = fixtures_dir

    def close(self) -> None:
        if not self._external_fetcher:
            self.fetcher.close()

    @classmethod
    def _parse_retrinapro_grid_items(
        cls,
        tree: HTMLParser,
        category: CategorySlug,
    ) -> list[ScrapedListing]:
        """Parse live MD Computers RetrinaPro `div.product-grid-item` cards."""
        listings: list[ScrapedListing] = []
        for card in tree.css("div.product-grid-item"):
            title_link = card.css_first("h3.product-entities-title a") or card.css_first(
                ".product-wrapper a.product-image-link"
            )
            if title_link is None:
                continue
            raw_title = title_link.text(strip=True)
            href = (title_link.attributes.get("href") or "").strip()
            if not raw_title or not href:
                continue

            canonical_url = _canonicalize_md_product_url(href)

            # Extract selling price (`span.price span.ins span.amount` or `span.price span.amount`)
            ins_node = card.css_first("span.price span.ins span.amount") or card.css_first(
                "span.price span.amount"
            )
            price_paise = parse_price_paise(ins_node.text(strip=True) if ins_node else None)
            if price_paise is None:
                continue

            del_node = card.css_first("span.price span.del span.amount")
            mrp_paise = parse_price_paise(del_node.text(strip=True) if del_node else None)

            # External ID from `cart.add('26371')`
            ext_id: str | None = None
            cart_btn = card.css_first("button[onclick*='cart.add']") or card.css_first(
                "[onclick*='cart.add']"
            )
            if cart_btn is not None:
                onclick = cart_btn.attributes.get("onclick") or ""
                m_id = _CART_ADD_RE.search(onclick)
                if m_id:
                    ext_id = m_id.group(1)

            # Image URL and MPN hint from catalog path
            img_node = card.css_first("a.product-image-link img") or card.css_first("img")
            image_url: str | None = None
            raw_mpn: str | None = None
            if img_node is not None:
                src = (
                    img_node.attributes.get("data-src") or img_node.attributes.get("src") or ""
                ).strip()
                if src and not src.startswith("data:"):
                    image_url = src
                    m_mpn = _IMG_MPN_RE.search(src)
                    if m_mpn:
                        candidate_mpn = m_mpn.group(1).upper()
                        if any(ch.isdigit() for ch in candidate_mpn):
                            raw_mpn = candidate_mpn

            card_classes = (card.attributes.get("class") or "").lower()
            card_text = card.text(separator=" ", strip=True).lower()
            in_stock = "out-of-stock" not in card_classes and "out of stock" not in card_text

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=canonical_url,
                        external_id=ext_id,
                        raw_title=raw_title,
                        raw_mpn=raw_mpn,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=in_stock,
                        image_url=image_url,
                    )
                )
            except Exception as exc:
                logger.debug("[mdcomputers] Skipping invalid grid item (%s): %s", canonical_url, exc)

        return listings

    @classmethod
    def _parse_opencart_layout_items(
        cls,
        tree: HTMLParser,
        category: CategorySlug,
    ) -> list[ScrapedListing]:
        """Parse classic OpenCart/Journal3 `div.product-layout` cards."""
        listings: list[ScrapedListing] = []
        for card in tree.css("div.product-layout"):
            ext_id = card.attributes.get("data-product-id")

            name_link = card.css_first(".caption .name a") or card.css_first("h4 a")
            if name_link is None:
                continue
            raw_title = name_link.text(strip=True)
            href = (name_link.attributes.get("href") or "").strip()
            if not raw_title or not href:
                continue

            price_node = (
                card.css_first(".price .price-new")
                or card.css_first(".price .price-normal")
                or card.css_first(".price")
            )
            price_paise = parse_price_paise(price_node.text(strip=True) if price_node else None)
            if price_paise is None:
                continue

            mrp_node = card.css_first(".price .price-old")
            mrp_paise = parse_price_paise(mrp_node.text(strip=True) if mrp_node else None)

            raw_mpn: str | None = None
            for sp in card.css(".caption .stats .stat-1 span"):
                cls_attr = sp.attributes.get("class") or ""
                if "stats-label" not in cls_attr:
                    candidate = sp.text(strip=True)
                    if candidate:
                        raw_mpn = candidate
                        break

            card_classes = (card.attributes.get("class") or "").lower()
            in_stock = "out-of-stock" not in card_classes
            for sp in card.css(".caption .stats .stat-2 span"):
                cls_attr = sp.attributes.get("class") or ""
                if "stats-label" not in cls_attr:
                    stock_txt = sp.text(strip=True).lower()
                    if stock_txt:
                        in_stock = "in stock" in stock_txt and "out" not in stock_txt
                        break

            img_node = card.css_first(".image img")
            image_url: str | None = None
            if img_node is not None:
                data_src = (img_node.attributes.get("data-src") or "").strip()
                src = (img_node.attributes.get("src") or "").strip()
                if data_src and not data_src.startswith("data:"):
                    image_url = data_src
                elif src and not src.startswith("data:"):
                    image_url = src

            try:
                listing = ScrapedListing(
                    retailer=cls.slug,
                    category=category,
                    url=_canonicalize_md_product_url(href),
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
                logger.debug("[mdcomputers] Skipping invalid card (%s): %s", href, exc)

        return listings

    @classmethod
    def parse_category_html(cls, html: str, category: CategorySlug) -> list[ScrapedListing]:
        """Parse MD Computers category HTML (supports both RetrinaPro `.product-grid-item` and `.product-layout`)."""
        tree = HTMLParser(html)
        grid_listings = cls._parse_retrinapro_grid_items(tree, category)
        if grid_listings:
            return grid_listings
        return cls._parse_opencart_layout_items(tree, category)

    @classmethod
    def parse_product_page_html(cls, html: str) -> dict[str, Any]:
        """Parse MD Computers product detail page for JSON-LD `@type: Product` and spec tables."""
        details: dict[str, Any] = {}
        ld_products = extract_jsonld_products(html)
        if ld_products:
            details.update(parse_jsonld_product_fields(ld_products[0]))

        tree = HTMLParser(html)
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

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch MD Computers category page and return up to `quota` seed-prioritized listings."""
        target_quota = quota if quota is not None else self.targets.get_quota(category)
        target_seeds = seeds if seeds is not None else self.targets.get_seeds(category)

        if self.fixtures_dir is not None:
            cat_fixture = self.fixtures_dir / f"{category}_category.html"
            sample_fixture = self.fixtures_dir / "processor_category_sample.html"
            fpath = cat_fixture if cat_fixture.exists() else sample_fixture
            if not fpath.exists():
                return []
            candidates = self.parse_category_html(fpath.read_text(encoding="utf-8"), category)
            return self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)

        path = MD_CATEGORY_PATHS[category]
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
