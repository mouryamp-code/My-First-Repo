"""Dispatcher and registry for per-category attribute extractors."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from pcforge_scraper.config import CategorySlug, RetailerSlug
from pcforge_scraper.normalize.attributes.base import ExtractionResult
from pcforge_scraper.normalize.attributes.case import extract_case_attributes
from pcforge_scraper.normalize.attributes.cooler import extract_cooler_attributes
from pcforge_scraper.normalize.attributes.cpu import extract_cpu_attributes
from pcforge_scraper.normalize.attributes.fan import extract_fan_attributes
from pcforge_scraper.normalize.attributes.gpu import extract_gpu_attributes
from pcforge_scraper.normalize.attributes.monitor import extract_monitor_attributes
from pcforge_scraper.normalize.attributes.motherboard import extract_motherboard_attributes
from pcforge_scraper.normalize.attributes.peripherals import (
    extract_controller_attributes,
    extract_headset_attributes,
    extract_keyboard_attributes,
    extract_mouse_attributes,
)
from pcforge_scraper.normalize.attributes.psu import extract_psu_attributes
from pcforge_scraper.normalize.attributes.ram import extract_ram_attributes
from pcforge_scraper.normalize.attributes.storage import (
    extract_hdd_attributes,
    extract_ssd_attributes,
)

ExtractorFn = Callable[..., ExtractionResult]

EXTRACTORS: dict[CategorySlug, ExtractorFn] = {
    "cpu": extract_cpu_attributes,
    "gpu": extract_gpu_attributes,
    "motherboard": extract_motherboard_attributes,
    "ram": extract_ram_attributes,
    "psu": extract_psu_attributes,
    "case": extract_case_attributes,
    "ssd": extract_ssd_attributes,
    "hdd": extract_hdd_attributes,
    "cooler": extract_cooler_attributes,
    "monitor": extract_monitor_attributes,
    "fan": extract_fan_attributes,
    "keyboard": extract_keyboard_attributes,
    "mouse": extract_mouse_attributes,
    "headset": extract_headset_attributes,
    "controller": extract_controller_attributes,
}


def infer_category_from_title(raw_title: str) -> CategorySlug:
    """Heuristically infer `CategorySlug` from a standalone product title (used by `pcforge-normalize --explain`)."""
    t = raw_title.lower()
    # Peripherals first: their titles often mention PC parts ("for PS5", "RGB", "1000Hz").
    if re.search(r"\b(?:controller|gamepad|game\s*pad|joystick|dualsense|dualshock)\b", t):
        return "controller"
    if re.search(r"\b(?:headset|headphones?|earphones?)\b", t):
        return "headset"
    if re.search(r"\bkeyboard\b", t) and not re.search(r"\bmouse\b", t):
        return "keyboard"
    if re.search(r"\bmouse\b", t) and not re.search(r"\bmouse\s*(?:pad|mat)\b", t):
        return "mouse"
    if re.search(r"\b(?:ryzen\s+[3579]|core\s+i[3579]|core\s+ultra|threadripper|athlon)\b", t) and not re.search(
        r"\bmotherboard\b", t
    ):
        return "cpu"
    if re.search(r"\b(?:rtx|gtx|radeon\s+rx|rx\s+\d{3,4}|arc\s+[ab]\d{3}|gt\s+\d{3,4}|graphics\s+card|graphic\s+card)\b", t):
        return "gpu"
    if re.search(r"\b(?:ddr[345])\b", t) and re.search(
        r"\b(?:\d{4}\s*(?:mhz|mt/s)|cl\d{2}|vengeance|ripjaws|trident|fury|lancer|xpg|delta|vulcan|ram)\b", t
    ) and not re.search(r"\b(?:motherboard|b650|b550|b760|h610|z790|a520|a620|b450)\b", t):
        return "ram"
    if re.search(
        r"\b(?:motherboard|b650m?|b550m?|b760m?|h610m?|z790|x670e?|x870e?|a620m?|a520m?|b450m?|a320m?)\b",
        t,
    ):
        return "motherboard"
    if re.search(r"\b(?:80\s*(?:\+|plus)|power\s+supply|smps|modular|\d{3,4}\s*w(?:att)?)\b", t):
        return "psu"
    if re.search(r"\b(?:nvme|ssd|solid\s+state|m\.2\s+2280|sn850x?|sn770|sn580|990\s+pro|980\s+pro)\b", t):
        return "ssd"
    if re.search(r"\b(?:hdd|hard\s+drive|barracuda|ironwolf|skyhawk|7200\s*rpm|5400\s*rpm)\b", t):
        return "hdd"
    if re.search(r"\b(?:monitor|ultragear|odyssey\s+g\d|\d{2,3}\s*hz|wqhd|fhd\s+ips|qhd\s+ips)\b", t):
        return "monitor"
    if re.search(r"\b(?:cpu\s+cooler|liquid\s+cooler|aio\s+cooler|ak400|ak620|ag400|le520|ls720|nh-d15)\b", t):
        return "cooler"
    if re.search(
        r"\b(?:case\s+fans?|cabinet\s+fans?|p1[24]\s+pwm|uni\s+fan|fans?\s+(?:single|dual|twin|triple|value)?\s*pack)\b",
        t,
    ):
        return "fan"
    return "case"


def extract_attributes(
    category: CategorySlug,
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Dispatch to the appropriate category attribute extractor."""
    extractor = EXTRACTORS[category]
    return extractor(
        raw_title,
        raw_brand=raw_brand,
        raw_mpn=raw_mpn,
        retailer=retailer,
        source_fields=source_fields,
    )


__all__ = [
    "EXTRACTORS",
    "ExtractionResult",
    "extract_attributes",
    "extract_case_attributes",
    "extract_cooler_attributes",
    "extract_cpu_attributes",
    "extract_fan_attributes",
    "extract_gpu_attributes",
    "extract_hdd_attributes",
    "extract_monitor_attributes",
    "extract_motherboard_attributes",
    "extract_psu_attributes",
    "extract_ram_attributes",
    "extract_ssd_attributes",
    "infer_category_from_title",
]
