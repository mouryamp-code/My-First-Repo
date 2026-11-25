"""Base retailer interface, ScrapedListing pydantic model, JSON-LD extractor, and quota selector."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from pcforge_scraper.config import (
    CORE_CATEGORIES,
    EXPANDED_CATEGORIES,
    CategorySlug,
    RetailerSlug,
    TargetsConfig,
    load_targets,
)
from pcforge_scraper.normalize.text import clean_product_url, clean_title_basic, parse_price_paise

_RENEWED_RE = re.compile(
    r"\b(?:renewed|refurbished|repack(?:ed)?|re[-\s]packed|pre[-\s]?owned|unboxed|open[-\s]?box|second[-\s]?hand|used)\b",
    re.IGNORECASE,
)

#: Title rules for category pages that mix product types (a "cooling" page with AIOs, case fans,
#: thermal paste...). Only applied where an adapter marks a route as mixed.
_MIXED_PAGE_RULES: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "cooler": (
        re.compile(r"\b(?:cpu\s*cooler|cooler|aio|liquid|heat\s*sink|heatsink|tower)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:thermal\s*(?:paste|pad|compound|grease)|headset|headphones?|bracket|mounting\s*kit|"
            r"fan\s*(?:pack|kit|hub|controller)|(?:single|dual|triple|twin|[2-9])\s*(?:fan\s*)?pack|"
            r"(?:case|cabinet|chassis)\s*fans?|laptop)\b",
            re.IGNORECASE,
        ),
    ),
    "fan": (
        re.compile(r"\bfans?\b", re.IGNORECASE),
        re.compile(
            r"\b(?:cpu\s*cooler|cooler|aio|liquid|heat\s*sink|heatsink|radiator|thermal|laptop|"
            r"hub|controller|headset)\b",
            re.IGNORECASE,
        ),
    ),
}


def belongs_on_mixed_page(category: str, title: str) -> bool:
    """True if `title` is really a `category` product, for routes that mix several types."""
    rule = _MIXED_PAGE_RULES.get(category)
    if rule is None:
        return True
    include, exclude = rule
    return bool(include.search(title)) and not exclude.search(title)


#: A category's source pages: one path, or several (e.g. air + liquid cooler sub-categories).
CategoryRoute = str | tuple[str, ...]


_COMBO_RE = re.compile(
    r"(?:\b(?:combo(?:\s+pack|\s+offer)?|bundle\s+pack|processor\s*(?:\+|with)\s*motherboard|"
    r"cpu\s*(?:\+|with)\s*motherboard|motherboard\s*(?:\+|with)\s*(?:processor|cpu|ryzen|core))\b|"
    r"\+\s*(?:b450|b550|b650|b760|h610|a520|a620|x670|z790)\b)",
    re.IGNORECASE,
)


def as_dict(val: Any) -> dict[str, Any]:
    """Return `val` if it is a dict, otherwise an empty dict."""
    return val if isinstance(val, dict) else {}


def as_list(val: Any) -> list[Any]:
    """Return `val` if it is a list, otherwise an empty list."""
    return val if isinstance(val, list) else []


def is_renewed_or_refurbished(
    title: str,
    condition: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> bool:
    """Return True if a listing title, condition, or attribute marks it as renewed/refurbished/used."""
    if _RENEWED_RE.search(title):
        return True
    if condition and condition.strip().lower() not in ("new", "oem", ""):
        return True
    if attributes:
        cond_val = str(attributes.get("condition") or "").strip().lower()
        if cond_val and cond_val not in ("new", "oem"):
            return True
    return False


def is_combo_bundle(title: str) -> bool:
    """Return True if a listing title represents a multi-component combo bundle (e.g. CPU + Motherboard)."""
    return bool(_COMBO_RE.search(title))


class ScrapedListing(BaseModel):
    """Validated raw product listing emitted by a retailer scraper or official API adapter."""

    retailer: RetailerSlug
    category: CategorySlug
    url: str
    external_id: str | None = None
    raw_title: str
    raw_brand: str | None = None
    raw_mpn: str | None = None
    price_paise: int = Field(gt=0, description="Selling price in integer INR paise (> 0)")
    mrp_paise: int | None = Field(default=None, gt=0, description="Optional MRP in integer INR paise")
    in_stock: bool = True
    image_url: str | None = None
    source_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _clean_url(cls, v: str) -> str:
        return clean_product_url(v)

    @field_validator("raw_title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        cleaned = " ".join(v.split()).strip()
        if not cleaned:
            raise ValueError("raw_title must not be empty")
        return cleaned

    @field_validator("raw_mpn")
    @classmethod
    def _clean_mpn(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip().upper()
        return cleaned if cleaned else None

    @model_validator(mode="after")
    def _normalize_mrp(self) -> ScrapedListing:
        if self.mrp_paise is not None:
            if self.mrp_paise <= self.price_paise or self.mrp_paise > int(self.price_paise * 2.5):
                self.mrp_paise = None
        return self


def extract_jsonld_products(html: str) -> list[dict[str, Any]]:
    """Extract all Schema.org `@type: Product` JSON-LD objects from an HTML document."""
    tree = HTMLParser(html)
    products: list[dict[str, Any]] = []

    def _collect(obj: Any) -> None:
        if isinstance(obj, dict):
            raw_type = obj.get("@type")
            types = [raw_type] if isinstance(raw_type, str) else (raw_type if isinstance(raw_type, list) else [])
            if any(str(t).lower().endswith("product") for t in types):
                products.append(obj)
            if "@graph" in obj and isinstance(obj["@graph"], list):
                for item in obj["@graph"]:
                    _collect(item)
            if "itemListElement" in obj and isinstance(obj["itemListElement"], list):
                for el in obj["itemListElement"]:
                    if isinstance(el, dict):
                        _collect(el.get("item", el))
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    for script in tree.css('script[type="application/ld+json"]'):
        raw_json = script.text(strip=True)
        if not raw_json:
            continue
        # Strip control characters that occasionally break json.loads in OpenCart templates
        cleaned_json = re.sub(r"[\x00-\x1f]+", " ", raw_json)
        try:
            parsed = json.loads(cleaned_json)
            _collect(parsed)
        except json.JSONDecodeError:
            continue

    return products


def parse_jsonld_product_fields(ld_obj: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized fields (`name`, `brand`, `mpn`, `sku`, `price_paise`, `in_stock`, `image_url`, `url`) from a JSON-LD Product dict."""
    result: dict[str, Any] = {}
    if isinstance(ld_obj.get("name"), str) and ld_obj["name"].strip():
        result["name"] = ld_obj["name"].strip()

    brand_val = ld_obj.get("brand")
    if isinstance(brand_val, dict):
        b_name = brand_val.get("name")
        if isinstance(b_name, str) and b_name.strip():
            result["brand"] = b_name.strip()
    elif isinstance(brand_val, str) and brand_val.strip():
        result["brand"] = brand_val.strip()

    for key in ("mpn", "sku", "productID"):
        val = ld_obj.get(key)
        if isinstance(val, str) and val.strip():
            result[key] = val.strip()

    img = ld_obj.get("image")
    if isinstance(img, str) and img.strip():
        result["image_url"] = img.strip()
    elif isinstance(img, list) and img:
        first_img = img[0]
        if isinstance(first_img, str) and first_img.strip():
            result["image_url"] = first_img.strip()
        elif isinstance(first_img, dict) and isinstance(first_img.get("url"), str):
            result["image_url"] = first_img["url"].strip()

    offers = ld_obj.get("offers")
    offer_dict: dict[str, Any] | None = None
    if isinstance(offers, dict):
        offer_dict = offers
    elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
        offer_dict = offers[0]

    if offer_dict is not None:
        raw_price = offer_dict.get("price") or offer_dict.get("lowPrice")
        price_paise = parse_price_paise(raw_price)
        if price_paise is not None:
            result["price_paise"] = price_paise
        avail = str(offer_dict.get("availability") or "").lower()
        if avail:
            result["in_stock"] = "instock" in avail and "outofstock" not in avail
        offer_url = offer_dict.get("url") or ld_obj.get("url")
        if isinstance(offer_url, str) and offer_url.strip():
            result["url"] = offer_url.strip()

    return result


def _redirected_home(requested: str, final: str | None) -> bool:
    if not final or final == requested:
        return False
    from urllib.parse import urlsplit

    req, got = urlsplit(requested), urlsplit(final)
    return req.path.strip("/") != "" and (got.path.strip("/") == "" or "route=common/home" in got.query)


class RetailerSource(ABC):
    """Abstract base class for scraped retailers and official API sources."""

    slug: RetailerSlug
    name: str
    base_url: str
    #: Categories this source can fetch. Stores without peripheral pages keep the default,
    #: so runs over every category simply skip what a store doesn't sell.
    supported_categories: tuple[CategorySlug, ...] = CORE_CATEGORIES + EXPANDED_CATEGORIES

    def supports(self, category: CategorySlug) -> bool:
        """Return True if this source has a page or feed for `category`."""
        return category in self.supported_categories

    def __init__(self, targets: TargetsConfig | None = None) -> None:
        self.targets = targets or load_targets()
        self.last_skipped_counts: dict[CategorySlug, int] = {}
        self.last_category_notes: dict[CategorySlug, str] = {}

    @abstractmethod
    def fetch_category(
        self,
        category: CategorySlug,
        quota: int | None = None,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch and parse listings for a single category honoring `quota` and `seeds`."""

    def fetch_all(
        self,
        categories: Iterable[CategorySlug] | None = None,
    ) -> list[ScrapedListing]:
        """Fetch listings across all requested categories (defaults to all 6 categories)."""
        cats = list(categories) if categories is not None else list(self.targets.quotas.keys())
        all_listings: list[ScrapedListing] = []
        for cat in cats:
            if not self.supports(cat):
                continue
            quota = self.targets.get_quota(cat)
            seeds = self.targets.get_seeds(cat)
            all_listings.extend(self.fetch_category(cat, quota=quota, seeds=seeds))
        return all_listings

    #: Safety cap for full-catalog crawls of paginated category pages.
    MAX_CATEGORY_PAGES: int = 60

    def crawl_category_pages(
        self,
        category: CategorySlug,
        page_url: Callable[[int], str],
        parse: Callable[[str], list[ScrapedListing]],
        fetch: Callable[[str], Any],
        max_pages: int | None = None,
    ) -> list[ScrapedListing]:
        """Walk page 1, 2, 3... of a category until a page adds nothing new.

        Stops on a non-200, an empty page, or a page whose products were all seen already
        (OpenCart stores repeat the last page for out-of-range page numbers).
        """
        candidates: list[ScrapedListing] = []
        seen: set[str] = set()
        for page_num in range(1, (max_pages or self.MAX_CATEGORY_PAGES) + 1):
            try:
                resp = fetch(page_url(page_num))
            except Exception as exc:  # keep what earlier pages gave us
                self.last_category_notes[category] = f"p{page_num}: {exc}"
                break
            if resp.status_code != 200:
                if page_num == 1:
                    self.last_category_notes[category] = f"HTTP {resp.status_code}"
                break
            if _redirected_home(page_url(page_num), getattr(resp, "url", None)):
                # A dead category path that bounces to the home page would otherwise fill the
                # category with whatever the home page features.
                self.last_category_notes[category] = "category page redirects to the home page"
                break
            fresh = [item for item in parse(resp.text) if item.url not in seen]
            if not fresh:
                break
            seen.update(item.url for item in fresh)
            candidates.extend(fresh)
        return candidates

    def crawl_routes(
        self,
        category: CategorySlug,
        route: CategoryRoute,
        page_url: Callable[[str, int], str],
        parse: Callable[[str], list[ScrapedListing]],
        fetch: Callable[[str], Any],
        mixed: bool = False,
    ) -> list[ScrapedListing]:
        """Full-catalog crawl of every path in `route`; `mixed` keeps only titles of `category`."""
        paths = (route,) if isinstance(route, str) else route
        out: list[ScrapedListing] = []
        seen: set[str] = set()
        for path in paths:
            for item in self.crawl_category_pages(category, lambda n, p=path: page_url(p, n), parse, fetch):
                if item.url in seen:
                    continue
                seen.add(item.url)
                if mixed and not belongs_on_mixed_page(category, item.raw_title):
                    continue
                out.append(item)
        return out

    @staticmethod
    def select_by_seeds_and_quota(
        candidates: Iterable[ScrapedListing],
        quota: int,
        seeds: list[str] | None = None,
    ) -> list[ScrapedListing]:
        """Deduplicate candidates by URL, pick seed-matching listings first, then fill in page order."""
        if quota <= 0:
            return []

        deduped: list[ScrapedListing] = []
        seen_urls: set[str] = set()
        for item in candidates:
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            deduped.append(item)

        normalized_seeds = [clean_title_basic(s) for s in (seeds or []) if s.strip()]
        if not normalized_seeds:
            return deduped[:quota]

        seed_matched: list[ScrapedListing] = []
        remaining: list[ScrapedListing] = []

        for item in deduped:
            norm_title = clean_title_basic(item.raw_title)
            if any(seed in norm_title for seed in normalized_seeds):
                seed_matched.append(item)
            else:
                remaining.append(item)

        combined = seed_matched + remaining
        return combined[:quota]
