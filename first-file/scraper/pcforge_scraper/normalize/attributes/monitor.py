"""Monitor attribute extractor."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import ExtractionResult
from pcforge_scraper.normalize.attributes.generic import fallback_model
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

_SCREEN_SIZE_RE = re.compile(
    r"\b(21\.5|22|23\.8|24|24\.5|25|26\.5|27|28|31\.5|32|34|38|43|45|49)(?:\s*[\-\"]?\s*(?:inch|in|\"))\b",
    re.IGNORECASE,
)

_MONITOR_MODEL_RE = re.compile(
    r"\b("
    r"2[247]\d?[a-z]{1,3}\d{2,4}[a-z0-9\-]*|"
    r"3[24]\d?[a-z]{1,3}\d{2,4}[a-z0-9\-]*|"
    r"vg\d{3,4}[a-z0-9\-]*|"
    r"xg\d{3,4}[a-z0-9\-]*|"
    r"pg\d{3,4}[a-z0-9\-]*|"
    r"g\d{3,4}[a-z0-9\-]*|"
    r"mag[\s\-]+2\d{2}[a-z0-9\-]*|"
    r"mpg[\s\-]+2\d{2}[a-z0-9\-]*|"
    r"gs\d{2}[a-z0-9\-]*|"
    r"m\d{2}[qx][a-z0-9\-]*|"
    r"xv\d{3}[a-z0-9\-]*|"
    r"nitro\s+[a-z]{2}\d{3}[a-z0-9\-]*|"
    r"ex\d{3,4}[a-z0-9\-]*|"
    r"gw\d{4}[a-z0-9\-]*|"
    r"xl\d{4}[a-z0-9\-]*|"
    r"vx\d{4}[a-z0-9\-]*|"
    r"odyssey\s+g\d[a-z0-9\-]*"
    r")\b",
    re.IGNORECASE,
)

# Sizes written as 27″ / 27" / 24.1-inch / "67.31cms (26.5)".
_ANY_SIZE_RE = re.compile(
    r"(?<![\d.])(\d{2}(?:\.\d)?)\s*(?:″|′′|′|”|\"|''|-?\s*inch(?:es)?\b|in\b)"
    r"|\bcms?\s*\(?(\d{2}(?:\.\d)?)\)?(?![\d.])",
    re.IGNORECASE,
)

_MONITOR_STOP_WORDS = frozenset(
    """
    monitor monitors display screen inch inches curved flat ultrawide ultra-wide gaming esports
    business professional office designer portable touch smart ips va tn oled qd-oled mini-led hdr
    full hd fhd qhd uhd wqhd 4k 2k 5k hz refresh frameless borderless speakers eye care
    """.split()
)


def _parse_any_screen_size(cleaned: str) -> str | None:
    for m in _ANY_SIZE_RE.finditer(cleaned):
        raw = m.group(1) or m.group(2)
        size = float(raw)
        if 13 <= size <= 57:
            text = f"{size:g}"
            return f"{text}in"
    return None


_REFRESH_RE = re.compile(r"\b(60|75|100|120|144|165|170|180|200|240|280|360|480)\s*hz\b", re.IGNORECASE)


def extract_monitor_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized Monitor attributes (`brand`, `model`, `screen_size`, `resolution`, `refresh_rate`, `panel_type`)."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="monitor", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "monitor", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    m_size = _SCREEN_SIZE_RE.search(cleaned)
    screen_size = f"{m_size.group(1)}in" if m_size else _parse_any_screen_size(cleaned)

    m_model = _MONITOR_MODEL_RE.search(cleaned)
    model = re.sub(r"\s+", "-", m_model.group(1).lower()) if m_model else None
    if model:
        # "27gr75q-b" == "27gr75q": a trailing "-b" is the black (default) colour code.
        # Other suffixes ("-w" white, "-qd" quantum dot) are real variants and are kept.
        model = model.removesuffix("-b")
    # Samsung model codes ("LS27BG400EWXXL") tell sizes and SKUs apart where "Odyssey G4" can't.
    # The letter after the digits matters: LS27FG502E is the G5, LS27FG502S the OLED G5.
    m_samsung = re.search(r"\bls(\d{2}[a-z]{1,3}\d{3}[a-z]?)", cleaned)
    if brand_slug == "samsung" and m_samsung:
        model = f"ls{m_samsung.group(1)}"
    elif model:
        # Keep short variant suffixes that are separate SKUs: "MAG 275QF E20" vs "X32", "XV272U F3".
        m_suffix = re.search(rf"{re.escape(model.replace('-', ' '))}\s+([a-z]\d{{1,2}})\b", cleaned)
        if m_suffix and not re.fullmatch(r"[gq]\d", m_suffix.group(1)):
            model = f"{model}-{m_suffix.group(1)}"
    if model is None and mpn:
        model = mpn.lower()
    if model is None and brand_slug:
        model = fallback_model(cleaned, brand_slug, _MONITOR_STOP_WORDS)

    if screen_size is None and model:
        m_prefix = re.match(r"^(22|24|25|27|28|32|34|49)", model)
        if m_prefix:
            screen_size = f"{m_prefix.group(1)}in"

    resolution: str | None = None
    if re.search(r"\b(?:4k|uhd|3840\s*x\s*2160)\b", cleaned):
        resolution = "4k"
    elif re.search(r"\b(?:wqhd|uwqhd|qhd|2k|2560\s*x\s*1440|3440\s*x\s*1440)\b", cleaned):
        resolution = "1440p"
    elif re.search(r"\b(?:fhd|full\s*hd|1080p|1920\s*x\s*1080)\b", cleaned):
        resolution = "1080p"

    m_hz = _REFRESH_RE.search(cleaned)
    refresh_rate = f"{m_hz.group(1)}hz" if m_hz else None

    panel_type: str | None = None
    if re.search(r"\b(?:qd[\s\-]*oled|oled)\b", cleaned):
        panel_type = "oled"
    elif re.search(r"\b(?:rapid\s*ips|fast\s*ips|ss\s*ips|nano\s*ips|ips)\b", cleaned):
        panel_type = "ips"
    elif re.search(r"\b(?:va|rapid\s*va)\b", cleaned):
        panel_type = "va"
    elif re.search(r"\btn\b", cleaned):
        panel_type = "tn"

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")
    if not screen_size:
        missing.append("screen_size")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "screen_size": screen_size,
        "resolution": resolution,
        "refresh_rate": refresh_rate,
        "panel_type": panel_type,
    }
    return ExtractionResult(
        category="monitor",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

