"""SSD and HDD storage attribute extractors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import (
    ExtractionResult,
    check_capacity_conflict,
)
from pcforge_scraper.normalize.attributes.generic import fallback_model
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

_STORAGE_CAP_RE = re.compile(
    r"\b(120|128|240|250|256|480|500|512|960|1000|1024|2000|2048|4000|4096)gb\b"
    r"|\b(1|2|4|6|8|10|12|14|16|18|20|22|24|26|28|30|32)\s*tb\b",
    re.IGNORECASE,
)

_SSD_MODEL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsn850x\b", re.I), "sn850x"),
    (re.compile(r"\bsn850\b", re.I), "sn850"),
    (re.compile(r"\bsn770\b", re.I), "sn770"),
    (re.compile(r"\bsn580\b", re.I), "sn580"),
    (re.compile(r"\bsn570\b", re.I), "sn570"),
    (re.compile(r"\bsn5000\b", re.I), "sn5000"),
    (re.compile(r"\bsn350\b", re.I), "sn350"),
    (re.compile(r"\b990\s*pro\b", re.I), "990-pro"),
    (re.compile(r"\b990\s*evo\s*plus\b", re.I), "990-evo-plus"),
    (re.compile(r"\b990\s*evo\b", re.I), "990-evo"),
    (re.compile(r"\b980\s*pro\b", re.I), "980-pro"),
    (re.compile(r"\b980\b", re.I), "980"),
    (re.compile(r"\b970\s*evo\s*plus\b", re.I), "970-evo-plus"),
    (re.compile(r"\b870\s*evo\b", re.I), "870-evo"),
    (re.compile(r"\b870\s*qvo\b", re.I), "870-qvo"),
    (re.compile(r"\bt705\b", re.I), "t705"),
    (re.compile(r"\bt700\b", re.I), "t700"),
    (re.compile(r"\bt500\b", re.I), "t500"),
    (re.compile(r"\bp3\s*plus\b", re.I), "p3-plus"),
    (re.compile(r"\bp310\b", re.I), "p310"),
    (re.compile(r"\bp3\b", re.I), "p3"),
    (re.compile(r"\bbx500\b", re.I), "bx500"),
    (re.compile(r"\bmx500\b", re.I), "mx500"),
    (re.compile(r"\bkc3000\b", re.I), "kc3000"),
    (re.compile(r"\bfury\s*renegade\b", re.I), "fury-renegade"),
    (re.compile(r"\bnv3\b", re.I), "nv3"),
    (re.compile(r"\bnv2\b", re.I), "nv2"),
    (re.compile(r"\ba400\b", re.I), "a400"),
    (re.compile(r"\bgammix\s*s70\s*blade\b", re.I), "gammix-s70-blade"),
    (re.compile(r"\bgammix\s*s60\b", re.I), "gammix-s60"),
    (re.compile(r"\blegend\s*960\b", re.I), "legend-960"),
    (re.compile(r"\blegend\s*850\b", re.I), "legend-850"),
    (re.compile(r"\blegend\s*800\b", re.I), "legend-800"),
    (re.compile(r"\bmp600\s*pro\b", re.I), "mp600-pro"),
    (re.compile(r"\bmp600\b", re.I), "mp600"),
    (re.compile(r"\bfirecuda\s*530\b", re.I), "firecuda-530"),
    (re.compile(r"\bbarracuda\s*q5\b", re.I), "barracuda-q5"),
    (re.compile(r"\bspatium\s*m480\b", re.I), "spatium-m480"),
    (re.compile(r"\bspatium\s*m450\b", re.I), "spatium-m450"),
    (re.compile(r"\bcardea\s*(?:z440|a440)\b", re.I), "cardea-a440"),
    (re.compile(r"\bmp33\s*pro\b", re.I), "mp33-pro"),
    (re.compile(r"\bmp33\b", re.I), "mp33"),
    (re.compile(r"\bmp44l\b", re.I), "mp44l"),
    (re.compile(r"\bmp44\b", re.I), "mp44"),
)

_HDD_SERIES_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bironwolf\s*pro\b", re.I), "ironwolf-pro"),
    (re.compile(r"\bironwolf\b", re.I), "ironwolf"),
    (re.compile(r"\bbarracuda\s*pro\b", re.I), "barracuda-pro"),
    (re.compile(r"\bbarracuda\b", re.I), "barracuda"),
    (re.compile(r"\bskyhawk\s*ai\b", re.I), "skyhawk-ai"),
    (re.compile(r"\bskyhawk\b", re.I), "skyhawk"),
    (re.compile(r"\bexos\b", re.I), "exos"),
    (re.compile(r"\bfirecuda\b", re.I), "firecuda"),
    (re.compile(r"\b(?:wd|western\s+digital)\s*red\s*pro\b", re.I), "wd-red-pro"),
    (re.compile(r"\bwd\s*red\s*plus\b", re.I), "wd-red-plus"),
    (re.compile(r"\bwd\s*red\b", re.I), "wd-red"),
    (re.compile(r"\bwd\s*black\b", re.I), "wd-black"),
    (re.compile(r"\bwd\s*blue\b|\bblue\b", re.I), "wd-blue"),
    (re.compile(r"\bwd\s*purple\b|\bpurple\b", re.I), "wd-purple"),
    (re.compile(r"\bwd\s*gold\b", re.I), "wd-gold"),
    (re.compile(r"\bultrastar\b", re.I), "ultrastar"),
    (re.compile(r"\bp300\b", re.I), "p300"),
    (re.compile(r"\bx300\b", re.I), "x300"),
    (re.compile(r"\bn300\b", re.I), "n300"),
)

_FALLBACK_ALNUM_MODEL_RE = re.compile(r"\b([a-z]{1,4}\d{2,4}[a-z0-9]*)\b", re.I)


def _normalize_storage_capacity(cleaned: str) -> str | None:
    m = _STORAGE_CAP_RE.search(cleaned)
    if not m:
        return None
    if m.group(1):
        gb = int(m.group(1))
        if gb in (1000, 1024):
            return "1tb"
        if gb in (2000, 2048):
            return "2tb"
        if gb in (4000, 4096):
            return "4tb"
        return f"{gb}gb"
    return f"{int(m.group(2))}tb"


# SATA-only product lines: their titles often omit the interface entirely.
_SATA_ONLY_MODELS = frozenset({"bx500", "mx500", "870-evo", "870-qvo", "a400"})


_SSD_STOP_WORDS = frozenset(
    """
    ssd ssds solid state drive drives disk nvme sata pcie m.2 m2 2280 2230 2242 internal
    read write speed speeds upto up mb/s gb/s heatsink with without gen gen3 gen4 gen5 x4 tlc qlc
    dram dram-less cache
    """.split()
)


def _extract_ssd_interface(cleaned: str, model: str | None = None) -> str:
    """Return `sata-m2`, `sata`, or `nvme[-genN]`.

    The PCIe generation is kept for display only; `build_match_key` collapses all NVMe
    variants to `nvme` because the model name already pins the generation.
    """
    is_m2 = bool(re.search(r"\bm\.?\s?2\b", cleaned))
    if re.search(r"\bsata\b", cleaned) or model in _SATA_ONLY_MODELS:
        return "sata-m2" if is_m2 else "sata"
    if re.search(r"\b(?:gen\s*5|pcie\s*5\.0|pcie\s*gen5)\b", cleaned):
        return "nvme-gen5"
    if re.search(r"\b(?:gen\s*4|pcie\s*4\.0|pcie\s*gen4)\b", cleaned):
        return "nvme-gen4"
    if re.search(r"\b(?:gen\s*3|pcie\s*3\.0|pcie\s*gen3)\b", cleaned):
        return "nvme-gen3"
    if re.search(r"\b(?:nvme|pcie)\b", cleaned) or is_m2:
        return "nvme"
    if re.search(r"\b2\.5\s*(?:inch|in|\")", cleaned):
        return "sata"
    return "nvme"


def extract_ssd_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized SSD attributes (`brand`, `model`, `capacity`, `interface`, `heatsink`)."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="ssd", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "ssd", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    capacity = _normalize_storage_capacity(cleaned)
    cap_conflict = check_capacity_conflict(capacity, source_fields, field_label="Storage capacity")
    if cap_conflict:
        conflicts.append(cap_conflict)

    model: str | None = None
    for pat, slug in _SSD_MODEL_PATTERNS:
        if pat.search(cleaned):
            model = slug
            break
    if model is None:
        for m_tok in _FALLBACK_ALNUM_MODEL_RE.finditer(cleaned):
            tok = m_tok.group(1).lower()
            if tok not in ("m2", "2280", "gen3", "gen4", "gen5", "sata3", "ddr4", "ddr5") and not tok.endswith(("gb", "tb")):
                model = tok
                break

    if model is None and brand_slug:
        model = fallback_model(cleaned, brand_slug, _SSD_STOP_WORDS, allow_numbers=True)

    interface = _extract_ssd_interface(cleaned, model)
    heatsink = bool(re.search(r"\bwith\s+heatsink\b|\bheatsink\b", cleaned)) and not bool(
        re.search(r"\bwithout\s+heatsink\b", cleaned)
    )

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")
    if not capacity:
        missing.append("capacity")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "capacity": capacity,
        "interface": interface,
        "heatsink": heatsink,
    }
    return ExtractionResult(
        category="ssd",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )


def extract_hdd_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized HDD attributes (`brand`, `series`, `capacity`, `rpm`)."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="hdd", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "hdd", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    capacity = _normalize_storage_capacity(cleaned)
    series: str | None = None
    for pat, slug in _HDD_SERIES_PATTERNS:
        if pat.search(cleaned):
            series = slug
            break
    if series is None and mpn:
        series = mpn.lower()

    rpm: str | None = None
    m_rpm = re.search(r"\b(5400|5900|7200)\s*rpm\b", cleaned)
    if m_rpm:
        rpm = f"{m_rpm.group(1)}rpm"

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not series:
        missing.append("series")
    if not capacity:
        missing.append("capacity")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "series": series,
        "capacity": capacity,
        "rpm": rpm,
    }
    return ExtractionResult(
        category="hdd",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

