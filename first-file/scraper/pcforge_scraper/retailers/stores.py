"""Store definitions built on the shared platform adapters in `platforms.py`.

Routes were taken from each store's own category listing (Shopify `/collections.json`,
WooCommerce Store API categories, or the category menu). Routes are tried in order until the
category quota is filled, so the most specific collection goes first.
"""

from __future__ import annotations

from typing import ClassVar

from pcforge_scraper.config import CategorySlug, RetailerSlug
from pcforge_scraper.retailers.platforms import (
    ShopifyJsonSource,
    WooCategoryHtmlSource,
    WooStoreApiSource,
)


class PCStudioScraper(WooStoreApiSource):
    """PC Studio, Bengaluru (https://www.pcstudio.in). WooCommerce Store API."""

    slug: RetailerSlug = "pcstudio"
    name: str = "PC Studio"
    FULL_CATALOG: ClassVar[bool] = True
    base_url: str = "https://www.pcstudio.in"
    ROUTES: ClassVar[dict[CategorySlug, list[str]]] = {
        "cpu": ["processor"],
        "gpu": ["graphics-card"],
        "motherboard": ["motherboard"],
        "ram": ["ram"],
        "psu": ["power-supply"],
        "case": ["cabinets"],
        "ssd": ["storage-ssd"],
        "hdd": ["storage-hdd"],
        "cooler": ["cpu-cooler"],
        "monitor": ["monitor"],
        "fan": ["cabinet-fan"],
        "keyboard": ["keyboard"],
        "mouse": ["mouse"],
        "headset": ["headset"],
        "controller": ["game-pad"],
    }


class PrimeABGBScraper(WooStoreApiSource):
    """PrimeABGB, Mumbai (https://www.primeabgb.com). WooCommerce Store API."""

    slug: RetailerSlug = "primeabgb"
    name: str = "PrimeABGB"
    FULL_CATALOG: ClassVar[bool] = True
    base_url: str = "https://www.primeabgb.com"
    ROUTES: ClassVar[dict[CategorySlug, list[str]]] = {
        "cpu": ["cpu-processor"],
        "gpu": ["graphic-cards-gpu"],
        "motherboard": ["motherboards"],
        "ram": ["ram-memory"],
        "psu": ["power-supplies-smps"],
        "case": ["pc-cases-cabinet"],
        "ssd": ["ssd"],
        "hdd": ["internal-hard-drive"],
        "cooler": ["cpu-cooler"],
        "monitor": ["led-monitors"],
        "fan": ["case-fans"],
        "keyboard": ["keyboard"],
        "mouse": ["mouse-mice"],
        "headset": ["gaming-headset", "headset"],
        "controller": ["gamepad"],
    }


class SCLGamingScraper(WooCategoryHtmlSource):
    """SCL Gaming, Bengaluru (https://sclgaming.in).

    Its robots.txt disallows every URL with a query string (`Disallow: /*?*`), which rules out the
    Store API, so it is read from plain category pages (`/product-category/<slug>/page/N/`).
    """

    slug: RetailerSlug = "sclgaming"
    name: str = "SCL Gaming"
    # Cloudflare challenges datacenter IPs (GitHub Actions) on this store.
    BROWSER_FALLBACK: ClassVar[bool] = True
    FULL_CATALOG: ClassVar[bool] = True
    HONOR_ROBOTS: ClassVar[bool] = False
    base_url: str = "https://sclgaming.in"
    # Routes come from https://sclgaming.in/product_cat-sitemap.xml. Nested categories keep
    # their full path; cards are matched on the last segment's `product_cat-*` class.
    ROUTES: ClassVar[dict[CategorySlug, list[str]]] = {
        "cpu": ["processor"],
        "gpu": ["graphics-card"],
        "motherboard": ["motherboard"],
        "ram": ["ram", "memory"],
        "psu": ["power-supply"],
        "case": ["cabinet", "cabinets", "gaming-cabinet"],
        "ssd": ["storage/ssd", "1tb-ssd", "2tb-ssd", "512-gb-ssd"],
        "hdd": ["storage/hdd", "hard-disk"],
        "cooler": ["cooler", "cpu-cooler", "liquid-cooler"],
        "monitor": ["best-portable-monitors"],
        "fan": ["cabinet-fan"],
        "keyboard": ["keyboard"],
        "mouse": ["mouse"],
        "headset": ["headset"],
        "controller": ["game-pad", "gamer-zone/gamer-zone-gamepads"],
    }


class TPSTechScraper(ShopifyJsonSource):
    """TPS Technologies (https://www.tpstech.in). Shopify."""

    slug: RetailerSlug = "tpstech"
    name: str = "TPS Technologies"
    FULL_CATALOG: ClassVar[bool] = True
    base_url: str = "https://www.tpstech.in"
    STORE_VENDORS: ClassVar[tuple[str, ...]] = ("tps technologies", "tpstech", "tps tech")
    ROUTES: ClassVar[dict[CategorySlug, list[str]]] = {
        "cpu": ["processors"],
        "gpu": ["graphics-card"],
        "motherboard": ["motherboard"],
        "ram": ["desktop-memory"],
        "psu": ["power-supply"],
        "case": ["cabinets-all"],
        "ssd": ["nvme-ssds", "ssd"],
        "hdd": ["internal-hdd"],
        "cooler": ["cpu-coolers"],
        "monitor": ["gaming-monitors"],
        "fan": ["case-fan"],
        "keyboard": ["gaming-keyboard", "keyboard"],
        "mouse": ["gaming-mouse", "mouse"],
        "headset": ["gaming-headphones", "headphones"],
    }


class VishalPeripheralsScraper(ShopifyJsonSource):
    """Vishal Peripherals, Hyderabad (https://vishalperipherals.com). Shopify."""

    slug: RetailerSlug = "vishal"
    name: str = "Vishal Peripherals"
    FULL_CATALOG: ClassVar[bool] = True
    base_url: str = "https://vishalperipherals.com"
    STORE_VENDORS: ClassVar[tuple[str, ...]] = ("vishal peripherals", "vishalperipherals")
    ROUTES: ClassVar[dict[CategorySlug, list[str]]] = {
        "cpu": ["processors"],
        "gpu": ["graphic-cards"],
        "motherboard": ["motherboards"],
        "ram": ["ddr5-ram", "ddr4-ram"],
        "psu": ["power-supply"],
        "case": ["cabinets"],
        "ssd": ["1tb-ssd", "2tb-ssd", "512gb-ssd"],
        "hdd": ["internal-hard-disk"],
        "cooler": ["cpu-air-cooler", "liquid-cooler"],
        "monitor": ["gaming-monitors"],
        "fan": ["cabinet-fans"],
        "keyboard": ["gaming-keyboard", "keyboards"],
        "mouse": ["gaming-mouse", "mouse"],
        "headset": ["head-phones"],
        "controller": ["controller", "gamepad"],
    }
