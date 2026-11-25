"""Retailer registry and factory."""

from __future__ import annotations

from pathlib import Path

from pcforge_scraper.config import RetailerSlug, TargetsConfig
from pcforge_scraper.retailers.amazon import AmazonScraper
from pcforge_scraper.retailers.base import RetailerSource, ScrapedListing
from pcforge_scraper.retailers.computech import ComputechScraper
from pcforge_scraper.retailers.elitehubs import EliteHubsScraper
from pcforge_scraper.retailers.flipkart import FlipkartScraper
from pcforge_scraper.retailers.mdcomputers import MDComputersScraper
from pcforge_scraper.retailers.stores import (
    PCStudioScraper,
    PrimeABGBScraper,
    SCLGamingScraper,
    TPSTechScraper,
    VishalPeripheralsScraper,
)
from pcforge_scraper.retailers.vedant import VedantScraper

SCRAPER_CLASSES: dict[str, type[RetailerSource]] = {
    "vedant": VedantScraper,
    "elitehubs": EliteHubsScraper,
    "mdcomputers": MDComputersScraper,
    "computech": ComputechScraper,
    "flipkart": FlipkartScraper,
    "amazon": AmazonScraper,
    "pcstudio": PCStudioScraper,
    "primeabgb": PrimeABGBScraper,
    "sclgaming": SCLGamingScraper,
    "tpstech": TPSTechScraper,
    "vishal": VishalPeripheralsScraper,
}


def get_retailer_source(
    slug: RetailerSlug | str,
    targets: TargetsConfig | None = None,
    fixtures_root: Path | None = None,
) -> RetailerSource:
    """Instantiate a RetailerSource by slug."""
    cls = SCRAPER_CLASSES.get(slug)
    if cls is None:
        raise ValueError(
            f"Unsupported retailer '{slug}'. Available sources: {sorted(SCRAPER_CLASSES)}"
        )
    fixtures_dir = (fixtures_root / slug) if fixtures_root is not None else None
    return cls(targets=targets, fixtures_dir=fixtures_dir)  # type: ignore[call-arg]


__all__ = [
    "SCRAPER_CLASSES",
    "AmazonScraper",
    "ComputechScraper",
    "EliteHubsScraper",
    "FlipkartScraper",
    "MDComputersScraper",
    "PCStudioScraper",
    "PrimeABGBScraper",
    "SCLGamingScraper",
    "TPSTechScraper",
    "VishalPeripheralsScraper",
    "RetailerSource",
    "ScrapedListing",
    "VedantScraper",
    "get_retailer_source",
]
