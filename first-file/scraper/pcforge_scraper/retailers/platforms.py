"""Reusable adapters for the e-commerce platforms most Indian PC stores run on.

Each store becomes a small subclass that only declares its category routes:

- `ShopifyJsonSource`: Shopify's public `/collections/<handle>/products.json` feed
  (250 products per request, structured prices, stock and vendor).
- `WooStoreApiSource`: WooCommerce's public Store API `/wp-json/wc/store/v1/products`
  (100 products per request, the same data the store's own cart uses).
- `WooCategoryHtmlSource`: plain WooCommerce category pages (`/product-category/<slug>/page/N/`)
  for stores whose robots.txt forbids query strings.

The JSON feeds are the lightest way to read a store: one request returns what would otherwise take
dozens of HTML page loads, so the store sees a small fraction of the traffic. Every request still goes
through `HttpFetcher`, which honours robots.txt, paces requests with randomized delays, backs off on
429/5xx and stops after repeated failures.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from pcforge_scraper.config import CategorySlug, TargetsConfig
from pcforge_scraper.fetch.http import HttpFetcher
from pcforge_scraper.normalize.text import parse_price_paise
from pcforge_scraper.retailers.base import (
    RetailerSource,
    ScrapedListing,
    is_combo_bundle,
    is_renewed_or_refurbished,
)

logger = logging.getLogger(__name__)

# Store "keyboard"/"mouse"/... collections are full of accessories that are not the product itself.
_ACCESSORY_GUARDS: dict[str, re.Pattern[str]] = {
    "keyboard": re.compile(
        r"\b(?:key\s*caps?|keycap\s*set|wrist\s*rest|palm\s*rest|mouse\s*(?:pad|mat)|mousepad|"
        r"combo|keyboard\s*(?:and|&|\+)\s*mouse|switch(?:es)?\s*(?:pack|set|tester)|\d+\s*pcs|"
        r"cable|coiled|dust\s*cover|keyboard\s*cover|stand|lube|puller|stabili[sz]ers?)\b",
        re.IGNORECASE,
    ),
    "mouse": re.compile(
        r"\b(?:mouse\s*(?:pad|mat|bungee|skates?|feet|grips?)|mousepad|desk\s*(?:pad|mat)|bungee|"
        r"skates?|grip\s*tape|combo|keyboard|charging\s*dock|receiver\s*only)\b",
        re.IGNORECASE,
    ),
    "headset": re.compile(
        r"\b(?:headset\s*stand|headphone\s*stand|hanger|ear\s*pads?|earpads?|cushions?|"
        r"replacement|speakers?|soundbar|sound\s*card|dac|amplifier|mic\s*arm|boom\s*arm|"
        r"extension\s*cable|splitter)\b",
        re.IGNORECASE,
    ),
    "controller": re.compile(
        r"\b(?:steering|wheel|pedals?|shifter|handbrake|cockpit|stand|charging\s*(?:dock|station|stand)|"
        r"thumb\s*grips?|grips?\s*kit|skin|carry(?:ing)?\s*case|bag|cable|battery\s*pack|"
        r"keyboard|mouse|stream\s*(?:controller|deck)|macro\s*(?:pad|keys))\b",
        re.IGNORECASE,
    ),
}

# Component categories: drop items stores file next to the real parts.
_ACCESSORY_GUARDS.update(
    {
        "hdd": re.compile(
            r"\b(?:external|portable|my\s+passport|expansion|elements|one\s+touch|backup\s+plus|"
            r"enclosure|docking|dock|caddy)\b",
            re.IGNORECASE,
        ),
        "ssd": re.compile(
            r"\b(?:external|portable|enclosure|docking|dock|caddy|adapter|heatsink\s+only|"
            r"usb\s*(?:3|c)\b.*\bssd\b.*\bportable)\b",
            re.IGNORECASE,
        ),
        "gpu": re.compile(r"\b(?:ai\s+box|egpu|riser|bracket|support\s+stand|holder)\b", re.IGNORECASE),
        "psu": re.compile(
            r"\b(?:cctv|\d+\s*channel|12\s*v\s*dc|dc\s+power\s+supply|adapter|laptop\s+charger|ups)\b",
            re.IGNORECASE,
        ),
        "case": re.compile(r"\b(?:rackmount|rack\s+mount|server\s+chassis|side\s+panel|riser\s+kit)\b", re.IGNORECASE),
        # "3 Fans with Controller" is a fan kit; a standalone "ARGB Fan Controller" / hub is not.
        "fan": re.compile(
            r"\b(?:fan\s+hub|splitter|extension\s+cable|fan\s+grill|dust\s+filter)\b"
            r"|^(?!.*\bwith\b).*\b(?:fan|argb|rgb)\s+controller\b",
            re.IGNORECASE,
        ),
    }
)

# Standalone microphones live in the same collections as headsets.
_STANDALONE_MIC_RE = re.compile(r"\bmic(?:rophone)?\b", re.IGNORECASE)
_HEAD_RE = re.compile(r"\bhead(?:set|phones?)\b|\bearphones?\b|\bearbuds?\b|\btws\b", re.IGNORECASE)


def is_off_category(title: str, category: CategorySlug) -> bool:
    """True if `title` is an accessory or bundle rather than a `category` product."""
    guard = _ACCESSORY_GUARDS.get(category)
    if guard and guard.search(title):
        return True
    if category == "headset" and _STANDALONE_MIC_RE.search(title) and not _HEAD_RE.search(title):
        return True
    return False


def should_skip_title(title: str, category: CategorySlug) -> bool:
    """Shared listing filter: refurbished, combo bundles and off-category accessories."""
    return is_renewed_or_refurbished(title) or is_combo_bundle(title) or is_off_category(title, category)


class _PlatformSource(RetailerSource):
    """Shared plumbing: fetcher lifecycle, fixtures, dedupe and quota selection."""

    #: Per-category list of store-side identifiers (collection handles, category slugs, ...).
    ROUTES: ClassVar[dict[CategorySlug, list[str]]] = {}
    #: Hard cap on pages per route, so a misbehaving feed can never loop forever.
    MAX_PAGES: ClassVar[int] = 10
    #: Fetch every product in the category instead of stopping at the quota. Worth it for JSON
    #: feeds (100-250 products per request): a store's whole PC catalog is only a few dozen
    #: requests, and listing everything is what lets one product be compared across stores.
    FULL_CATALOG: ClassVar[bool] = False
    #: Escalate to a real browser (Playwright) when the store answers with a Cloudflare challenge.
    BROWSER_FALLBACK: ClassVar[bool] = False
    #: False only for stores the owner chose to crawl regardless of robots.txt.
    HONOR_ROBOTS: ClassVar[bool] = True

    def __init__(
        self,
        targets: TargetsConfig | None = None,
        fetcher: HttpFetcher | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        super().__init__(targets=targets)
        self._external_fetcher = fetcher is not None
        self.fetcher = fetcher or HttpFetcher(
            retailer=self.slug, allow_browser_fallback=self.BROWSER_FALLBACK, honor_robots=self.HONOR_ROBOTS
        )
        self.fixtures_dir = fixtures_dir
        self.supported_categories = tuple(self.ROUTES.keys())

    def close(self) -> None:
        if not self._external_fetcher:
            self.fetcher.close()

    def _collect_target(self, quota: int) -> int:
        # Collect more than the quota so seed-matching popular models can be prioritised.
        return max(int(quota * 1.5), quota + 20)

    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        target_quota = quota if quota is not None else self.targets.get_quota(category)
        target_seeds = seeds if seeds is not None else self.targets.get_seeds(category)
        if not self.supports(category):
            return []

        if self.fixtures_dir is not None:
            candidates = self._load_fixture(category)
        else:
            want = 10**9 if self.FULL_CATALOG else self._collect_target(target_quota)
            candidates = self._fetch_live(category, want)
        if self.FULL_CATALOG:
            target_quota = max(target_quota, len(candidates))
        return self.select_by_seeds_and_quota(candidates, quota=target_quota, seeds=target_seeds)

    def _fetch_live(self, category: CategorySlug, want: int) -> list[ScrapedListing]:
        candidates: list[ScrapedListing] = []
        seen: set[str] = set()
        skipped = 0
        for route in self.ROUTES[category]:
            for page in range(1, self.MAX_PAGES + 1):
                try:
                    batch, page_skipped, has_more = self._fetch_page(category, route, page)
                except Exception as exc:  # one bad route shouldn't sink the category
                    self.last_category_notes[category] = f"{route} p{page}: {exc}"
                    logger.warning("[%s] %s page %d failed: %s", self.slug, route, page, exc)
                    break
                skipped += page_skipped
                fresh = [item for item in batch if item.url not in seen]
                seen.update(item.url for item in fresh)
                candidates.extend(fresh)
                if not has_more or not fresh or len(candidates) >= want:
                    break
            if len(candidates) >= want:
                break
        self.last_skipped_counts[category] = skipped
        return candidates

    def _load_fixture(self, category: CategorySlug) -> list[ScrapedListing]:
        raise NotImplementedError

    def _fetch_page(
        self, category: CategorySlug, route: str, page: int
    ) -> tuple[list[ScrapedListing], int, bool]:
        """Return `(listings, skipped_count, has_more_pages)` for one page of one route."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------


class ShopifyJsonSource(_PlatformSource):
    """Shopify store read through `/collections/<handle>/products.json`."""

    PAGE_SIZE: ClassVar[int] = 250
    MAX_PAGES: ClassVar[int] = 8
    #: Vendor names that are really the store itself (not a product brand).
    STORE_VENDORS: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def parse_products_json(
        cls, payload: dict[str, Any], category: CategorySlug, base_url: str
    ) -> tuple[list[ScrapedListing], int]:
        listings: list[ScrapedListing] = []
        skipped = 0
        for prod in payload.get("products") or []:
            title = html.unescape(str(prod.get("title") or "")).strip()
            handle = str(prod.get("handle") or "").strip()
            if not title or not handle or should_skip_title(title, category):
                skipped += 1
                continue

            variants = [v for v in prod.get("variants") or [] if isinstance(v, dict)]
            available = [v for v in variants if v.get("available")]
            pool = available or variants
            priced: list[tuple[int, dict[str, Any]]] = [
                (p, v) for v in pool if (p := parse_price_paise(v.get("price")))
            ]
            if not priced:
                skipped += 1
                continue
            price_paise, variant = min(priced, key=lambda pv: pv[0])
            mrp_paise = parse_price_paise(variant.get("compare_at_price"))

            images = prod.get("images") or []
            image_url = images[0].get("src") if images and isinstance(images[0], dict) else None

            vendor = str(prod.get("vendor") or "").strip()
            if vendor.lower() in cls.STORE_VENDORS:
                vendor = ""
            sku = str(variant.get("sku") or "").strip() or None

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=f"{base_url}/products/{handle}",
                        external_id=str(prod.get("id") or "") or None,
                        raw_title=title,
                        raw_brand=vendor or None,
                        raw_mpn=sku,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=bool(available),
                        image_url=image_url,
                        source_fields={
                            "vendor": vendor,
                            "product_type": prod.get("product_type"),
                            "tags": prod.get("tags"),
                        },
                    )
                )
            except Exception as exc:
                skipped += 1
                logger.debug("[%s] skipping %s: %s", cls.slug, handle, exc)
        return listings, skipped

    def _fetch_page(self, category: CategorySlug, route: str, page: int):  # type: ignore[override]
        url = f"{self.base_url}/collections/{route}/products.json"
        resp = self.fetcher.get(url, params={"limit": self.PAGE_SIZE, "page": page})
        if resp.status_code == 404:
            return [], 0, False
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        payload = resp.json()
        listings, skipped = self.parse_products_json(payload, category, self.base_url)
        has_more = len(payload.get("products") or []) >= self.PAGE_SIZE
        return listings, skipped, has_more

    def _load_fixture(self, category: CategorySlug) -> list[ScrapedListing]:
        import json

        path = self.fixtures_dir / f"{category}_products.json"  # type: ignore[operator]
        if not path.exists():
            return []
        listings, skipped = self.parse_products_json(
            json.loads(path.read_text(encoding="utf-8")), category, self.base_url
        )
        self.last_skipped_counts[category] = skipped
        return listings


# ---------------------------------------------------------------------------
# WooCommerce Store API
# ---------------------------------------------------------------------------


def _minor_to_paise(raw: Any, minor_unit: int) -> int | None:
    """Store API prices are integer strings in the currency's minor unit (e.g. "105000" with unit 2)."""
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Convert to paise (minor unit 2).
    return value * 10 ** (2 - minor_unit) if minor_unit <= 2 else value // 10 ** (minor_unit - 2)


class WooStoreApiSource(_PlatformSource):
    """WooCommerce store read through the public Store API."""

    PAGE_SIZE: ClassVar[int] = 100
    MAX_PAGES: ClassVar[int] = 15

    @classmethod
    def parse_store_api(
        cls, items: list[dict[str, Any]], category: CategorySlug
    ) -> tuple[list[ScrapedListing], int]:
        listings: list[ScrapedListing] = []
        skipped = 0
        for item in items:
            title = html.unescape(re.sub(r"<[^>]+>", "", str(item.get("name") or ""))).strip()
            url = str(item.get("permalink") or "").strip()
            if not title or not url or should_skip_title(title, category):
                skipped += 1
                continue
            if item.get("type") not in (None, "simple", "variable"):
                skipped += 1  # grouped/external products are not real offers
                continue

            prices = item.get("prices") or {}
            minor = int(prices.get("currency_minor_unit") or 0)
            price_paise = _minor_to_paise(prices.get("price"), minor)
            if price_paise is None:
                skipped += 1
                continue
            mrp_paise = _minor_to_paise(prices.get("regular_price"), minor)

            images = item.get("images") or []
            image_url = images[0].get("src") if images and isinstance(images[0], dict) else None
            brands = item.get("brands") or []
            brand = brands[0].get("name") if brands and isinstance(brands[0], dict) else None

            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=url,
                        external_id=str(item.get("id") or "") or None,
                        raw_title=title,
                        raw_brand=html.unescape(brand) if brand else None,
                        raw_mpn=str(item.get("sku") or "").strip() or None,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock=bool(item.get("is_in_stock")),
                        image_url=image_url,
                        source_fields={
                            "categories": [c.get("slug") for c in item.get("categories") or [] if isinstance(c, dict)],
                        },
                    )
                )
            except Exception as exc:
                skipped += 1
                logger.debug("[%s] skipping %s: %s", cls.slug, url, exc)
        return listings, skipped

    def _fetch_page(self, category: CategorySlug, route: str, page: int):  # type: ignore[override]
        url = f"{self.base_url}/wp-json/wc/store/v1/products"
        resp = self.fetcher.get(
            url,
            params={"category": route, "per_page": self.PAGE_SIZE, "page": page},
            extra_headers={"Accept": "application/json"},
        )
        if resp.status_code == 400 and page > 1:
            return [], 0, False  # past the last page
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        items = resp.json()
        if not isinstance(items, list):
            raise RuntimeError("unexpected Store API payload")
        listings, skipped = self.parse_store_api(items, category)
        total_pages = int(resp.headers.get("x-wp-totalpages") or resp.headers.get("X-WP-TotalPages") or 0)
        has_more = page < total_pages if total_pages else len(items) >= self.PAGE_SIZE
        return listings, skipped, has_more

    def _load_fixture(self, category: CategorySlug) -> list[ScrapedListing]:
        import json

        path = self.fixtures_dir / f"{category}_store_api.json"  # type: ignore[operator]
        if not path.exists():
            return []
        listings, skipped = self.parse_store_api(json.loads(path.read_text(encoding="utf-8")), category)
        self.last_skipped_counts[category] = skipped
        return listings


# ---------------------------------------------------------------------------
# WooCommerce category pages (HTML)
# ---------------------------------------------------------------------------


class WooCategoryHtmlSource(_PlatformSource):
    """WooCommerce store read from `/product-category/<slug>/page/N/` HTML pages."""

    MAX_PAGES: ClassVar[int] = 40

    @classmethod
    def parse_category_html(
        cls, page_html: str, category: CategorySlug, store_category: str
    ) -> tuple[list[ScrapedListing], int]:
        tree = HTMLParser(page_html)
        listings: list[ScrapedListing] = []
        skipped = 0
        seen: set[str] = set()
        # Nested routes ("storage/ssd") tag their cards with the last segment ("product_cat-ssd").
        cat_class = f"product_cat-{store_category.rstrip('/').split('/')[-1]}"

        for card in tree.css(".type-product"):
            classes = card.attributes.get("class") or ""
            # Pages also carry "related"/"trending" carousels from other categories.
            if cat_class not in classes.split():
                continue
            title_link = (
                card.css_first(".woocommerce-loop-product__title a")
                or card.css_first("h2 a[href*='/product/']")
                or card.css_first("h3 a[href*='/product/']")
            )
            title_node = title_link or card.css_first(".woocommerce-loop-product__title")
            link = title_link or card.css_first("a[href*='/product/']")
            if title_node is None or link is None:
                continue
            title = " ".join(title_node.text(strip=True).split())
            href = urljoin(cls.base_url, (link.attributes.get("href") or "").strip())
            if not title or not href or href in seen:
                continue
            seen.add(href)
            if should_skip_title(title, category):
                skipped += 1
                continue

            price_box = card.css_first(".price")
            price_paise = mrp_paise = None
            if price_box is not None:
                sale = price_box.css_first("ins .woocommerce-Price-amount")
                regular = price_box.css_first("del .woocommerce-Price-amount")
                current = sale or price_box.css_first(".woocommerce-Price-amount")
                price_paise = parse_price_paise(current.text(strip=True)) if current is not None else None
                mrp_paise = parse_price_paise(regular.text(strip=True)) if regular else None
            if price_paise is None:
                skipped += 1
                continue

            img = card.css_first("img")
            image_url = None
            if img is not None:
                src = (img.attributes.get("data-src") or img.attributes.get("src") or "").strip()
                if src and not src.startswith("data:"):
                    image_url = urljoin(cls.base_url, src)

            brand_match = re.search(r"\bpa_brand-([a-z0-9-]+)", classes)
            try:
                listings.append(
                    ScrapedListing(
                        retailer=cls.slug,
                        category=category,
                        url=href,
                        raw_title=title,
                        raw_brand=brand_match.group(1).replace("-", " ") if brand_match else None,
                        price_paise=price_paise,
                        mrp_paise=mrp_paise,
                        in_stock="outofstock" not in classes.split(),
                        image_url=image_url,
                        source_fields={"store_category": store_category},
                    )
                )
            except Exception as exc:
                skipped += 1
                logger.debug("[%s] skipping %s: %s", cls.slug, href, exc)
        return listings, skipped

    def _fetch_page(self, category: CategorySlug, route: str, page: int):  # type: ignore[override]
        path = f"/product-category/{route}/" + (f"page/{page}/" if page > 1 else "")
        resp = self.fetcher.get(f"{self.base_url}{path}")
        if resp.status_code == 404:
            return [], 0, False
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        listings, skipped = self.parse_category_html(resp.text, category, route)
        has_more = f"/product-category/{route}/page/{page + 1}/" in resp.text
        return listings, skipped, has_more

    def _load_fixture(self, category: CategorySlug) -> list[ScrapedListing]:
        path = self.fixtures_dir / f"{category}_category.html"  # type: ignore[operator]
        if not path.exists():
            return []
        route = self.ROUTES[category][0]
        listings, skipped = self.parse_category_html(path.read_text(encoding="utf-8"), category, route)
        self.last_skipped_counts[category] = skipped
        return listings
