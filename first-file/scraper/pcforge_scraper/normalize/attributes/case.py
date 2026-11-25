"""Case (PC Cabinet) attribute extractor (`Architecture.md` §5.3).

Required fields:
- `brand`: `lian-li`, `nzxt`, `cooler-master`, `deepcool`, `corsair`, `ant-esports`, `antec`, `xpg`, `aerocool`, etc.
- `model`: `lancool-216`, `h5-flow`, `4000d-airflow`, `crystal-x7`, `battlecruiser`, `si28`, `st10m`, etc.
- `form_factor`: `atx` | `matx` | `itx` | `eatx`
- `colour`: `black` | `white`

Optional flags:
- `argb_fans`: `bool`
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import ExtractionResult, check_colour_conflict
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

_CASE_STRIP_TOKENS = frozenset(
    {
        "atx",
        "matx",
        "itx",
        "eatx",
        "mid",
        "tower",
        "mid-tower",
        "mini",
        "mini-tower",
        "full",
        "full-tower",
        "micro",
        "gaming",
        "pc",
        "computer",
        "case",
        "cabinet",
        "chassis",
        "with",
        "type-c",
        "type",
        "c",
        "tempered",
        "glass",
        "side",
        "panel",
        "high",
        "airflow",
        "black",
        "white",
        "grey",
        "gray",
        "silver",
        "argb",
        "rgb",
        "fans",
        "fan",
        "adata",
        "xpg",
        "ant",
        "esports",
        "cooler",
        "master",
        "lian",
        "li",
        "fractal",
        "design",
        "aerocool",
        "antec",
        "corsair",
        "nzxt",
        "deepcool",
        "msi",
        "asus",
        "gigabyte",
        "thermaltake",
        "montech",
        "phanteks",
    }
)


def _extract_case_form_factor(cleaned: str) -> str | None:
    if re.search(r"\b(?:eatx|full[\s\-]+tower)\b", cleaned):
        return "eatx"
    if re.search(r"\bitx\b", cleaned):
        return "itx"
    if re.search(r"\b(?:matx|mini[\s\-]+tower)\b", cleaned):
        return "matx"
    if re.search(r"\b(?:atx|mid[\s\-]+tower)\b", cleaned):
        return "atx"
    return None


def _extract_case_model(cleaned: str, brand_slug: str | None) -> str | None:
    # Preserve `crystal ... mini` when `mini` is part of the model name (`Crystal X6 Mini`, `Crystal Neo Mini`)
    work = cleaned
    work = re.sub(r"\b(?:mid|full|mini)[\s\-]+tower\b", " ", work)
    work = re.sub(r"\bwith\s+type[\s\-]*c\b", " ", work)

    raw_tokens = re.split(r"[\s\-_.]+", work)
    kept: list[str] = []
    for tok in raw_tokens:
        t = tok.strip().lower()
        if not t:
            continue
        if t == "mini" and kept and kept[0] == "crystal":
            kept.append(t)
            continue
        if t == "airflow" and kept and kept[-1] in ("3000d", "4000d", "5000d", "7000d"):
            kept.append(t)
            continue
        if t in _CASE_STRIP_TOKENS:
            continue
        if brand_slug and t in brand_slug.split("-"):
            continue
        kept.append(t)

    if not kept:
        return None
    return "-".join(kept[:4])


def extract_case_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized Case attributes and check for structured field conflicts."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="case", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "case", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    model = _extract_case_model(cleaned, brand_slug)
    form_factor = _extract_case_form_factor(cleaned)

    # Default colour is `black` unless `white` is stated (§5.3 Note)
    is_white = bool(re.search(r"\b(?:white|snow|ice)\b", cleaned))
    colour = "white" if is_white else "black"

    argb_fans = bool(re.search(r"\b(?:argb|rgb)\b", cleaned))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")
    if not form_factor:
        missing.append("form_factor")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "form_factor": form_factor,
        "colour": colour,
        "white": is_white,
        "argb_fans": argb_fans,
    }
    return ExtractionResult(
        category="case",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

