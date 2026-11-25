"""RAM attribute extractor (`Architecture.md` §5.3).

Required fields:
- `brand`: `corsair`, `gskill`, `kingston`, `crucial`, `xpg`, `teamgroup`, etc.
- `series`: `vengeance`, `vengeance-lpx`, `trident-z5`, `ripjaws-s5`, `ripjaws-v`, `flare-x5`, `fury-beast`, `lancer-blade`, `delta`, `vulcan`, etc.
- `ram_type`: `ddr4` | `ddr5` | `ddr3`
- `total_capacity`: `8gb`, `16gb`, `32gb`, `64gb`, etc.
- `kit_config`: `1x8gb`, `2x8gb`, `1x16gb`, `2x16gb`, `1x32gb`, `2x32gb`, etc.
- `speed`: `3200`, `3600`, `4800`, `5200`, `5600`, `6000`, `6400`, etc.

Optional flags:
- `cas_latency`: `cl<NN>` (included only when present in the title; a listing without latency is `REVIEW` against a keyed one)
- `white`: `bool`
- `rgb`: `bool`
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import (
    ExtractionResult,
    check_capacity_conflict,
    check_colour_conflict,
)
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

_RAM_SERIES_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdominator[\s\-]+titanium\b", re.I), "dominator-titanium"),
    (re.compile(r"\bdominator[\s\-]+platinum\b|\bdominator\b", re.I), "dominator-platinum"),
    (re.compile(r"\bvengeance[\s\-]+rgb[\s\-]+pro\b|\bvengeance[\s\-]+pro\b", re.I), "vengeance-pro"),
    (re.compile(r"\bvengeance[\s\-]+lpx\b", re.I), "vengeance-lpx"),
    (re.compile(r"\bvengeance\b", re.I), "vengeance"),
    (re.compile(r"\btrident[\s\-]+z5[\s\-]+neo\b", re.I), "trident-z5-neo"),
    (re.compile(r"\btrident[\s\-]+z5[\s\-]+royal\b", re.I), "trident-z5-royal"),
    (re.compile(r"\btrident[\s\-]+z5\b", re.I), "trident-z5"),
    (re.compile(r"\btrident[\s\-]+z[\s\-]+neo\b", re.I), "trident-z-neo"),
    (re.compile(r"\btrident[\s\-]+z\b", re.I), "trident-z"),
    (re.compile(r"\bripjaws[\s\-]+s5\b", re.I), "ripjaws-s5"),
    (re.compile(r"\bripjaws[\s\-]+m5\b", re.I), "ripjaws-m5"),
    (re.compile(r"\bripjaws[\s\-]+v\b", re.I), "ripjaws-v"),
    (re.compile(r"\bripjaws\b", re.I), "ripjaws"),
    (re.compile(r"\bflare[\s\-]+x5\b", re.I), "flare-x5"),
    (re.compile(r"\baegis\b", re.I), "aegis"),
    (re.compile(r"\bfury[\s\-]+renegade\b", re.I), "fury-renegade"),
    (re.compile(r"\bfury[\s\-]+beast\b|\bbeast\b", re.I), "fury-beast"),
    (re.compile(r"\bfury[\s\-]+impact\b", re.I), "fury-impact"),
    (re.compile(r"\blancer[\s\-]+blade\b", re.I), "lancer-blade"),
    (re.compile(r"\blancer[\s\-]+neon\b", re.I), "lancer-neon"),
    (re.compile(r"\blancer\b", re.I), "lancer"),
    (re.compile(r"\bspectrix[\s\-]+d60g\b|\bd60g\b", re.I), "spectrix-d60g"),
    (re.compile(r"\bspectrix[\s\-]+d50\b|\bd50\b", re.I), "spectrix-d50"),
    (re.compile(r"\bspectrix[\s\-]+d45g\b|\bd45g\b", re.I), "spectrix-d45g"),
    (re.compile(r"\bspectrix[\s\-]+d35g\b|\bd35g\b", re.I), "spectrix-d35g"),
    (re.compile(r"\bgammix[\s\-]+d35\b", re.I), "gammix-d35"),
    (re.compile(r"\bgammix[\s\-]+d30\b|\bd30\b", re.I), "gammix-d30"),
    # Vedant spells XPG ARMAX as "AMRAX"; unify both to `armax`
    (re.compile(r"\b(?:armax|amrax)\b", re.I), "armax"),
    (re.compile(r"\bdelta\b", re.I), "delta"),
    (re.compile(r"\bvulcan[\s\-]+z\b", re.I), "vulcan-z"),
    (re.compile(r"\bvulcan\b", re.I), "vulcan"),
    (re.compile(r"\bt[\s\-]*create[\s\-]+expert\b", re.I), "t-create-expert"),
    (re.compile(r"\bt[\s\-]*create\b", re.I), "t-create"),
    (re.compile(r"\bxtreem\b", re.I), "xtreem"),
    (re.compile(r"\bballistix\b", re.I), "ballistix"),
    (re.compile(r"\bcrucial[\s\-]+pro\b", re.I), "pro"),
)

_KIT_RE = re.compile(r"\b(?P<sticks>[1248])x(?P<each>4|8|16|24|32|48|64)gb\b", re.IGNORECASE)
_CAP_RE = re.compile(r"(?<![x0-9])\b(4|8|16|24|32|48|64|96|128)gb\b", re.IGNORECASE)
_SPEED_RE = re.compile(
    r"\b(2400|2666|3000|3200|3600|4000|4800|5200|5600|6000|6200|6400|6600|6800|7000|7200|7600|8000)\b"
)
_CL_RE = re.compile(r"\b(cl\d{2})\b", re.IGNORECASE)


def _extract_ram_series(cleaned: str, brand_slug: str | None) -> str | None:
    base_series: str | None = None
    for pat, series_slug in _RAM_SERIES_PATTERNS:
        if pat.search(cleaned):
            base_series = series_slug
            break

    if base_series is None and brand_slug in ("crucial", "xpg", "kingston"):
        # Standard non-heatsink JEDEC modules (e.g. "Adata AD5U560016G-S 16GB DDR5", "Crucial 8GB DDR4")
        base_series = "standard"

    if base_series is not None and re.search(r"\b(?:laptop|sodimm|so-dimm)\b", cleaned):
        return f"{base_series}-sodimm"
    return base_series


def extract_ram_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized RAM attributes and check for structured field conflicts."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="ram", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "ram", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    series = _extract_ram_series(cleaned, brand_slug)

    ram_type: str | None = None
    if re.search(r"\bddr5\b", cleaned):
        ram_type = "ddr5"
    elif re.search(r"\bddr4\b", cleaned):
        ram_type = "ddr4"
    elif re.search(r"\bddr3\b", cleaned):
        ram_type = "ddr3"

    # Extract kit configuration and total capacity
    m_kit = _KIT_RE.search(cleaned)
    m_cap = _CAP_RE.search(cleaned)

    total_capacity: str | None = None
    kit_config: str | None = None

    if m_kit:
        sticks = int(m_kit.group("sticks"))
        each_gb = int(m_kit.group("each"))
        computed_total = f"{sticks * each_gb}gb"
        kit_config = f"{sticks}x{each_gb}gb"
        if m_cap:
            stated_total = f"{int(m_cap.group(1))}gb"
            if stated_total != computed_total:
                conflicts.append(
                    f"RAM kit/capacity conflict: stated total '{stated_total}' != kit '{kit_config}' ({computed_total})"
                )
            total_capacity = stated_total
        else:
            total_capacity = computed_total
    elif m_cap:
        cap_num = int(m_cap.group(1))
        total_capacity = f"{cap_num}gb"
        # If title says "dual channel kit" or "kit of 2" without per-stick breakdown, deduce 2x(cap/2)
        if re.search(r"\b(?:kit\s+of\s+2|dual\s+channel\s+kit)\b", cleaned) and cap_num % 2 == 0:
            kit_config = f"2x{cap_num // 2}gb"
        else:
            kit_config = f"1x{cap_num}gb"

    cap_conflict = check_capacity_conflict(total_capacity, source_fields, field_label="RAM capacity")
    if cap_conflict:
        conflicts.append(cap_conflict)

    m_speed = _SPEED_RE.search(cleaned)
    speed = m_speed.group(1) if m_speed else None

    # CAS latency is extracted strictly from the title (§5.3 Note)
    m_cl = _CL_RE.search(cleaned)
    cas_latency = m_cl.group(1).lower() if m_cl else None

    white = bool(re.search(r"\bwhite\b", cleaned))
    rgb = bool(re.search(r"\b(?:rgb|argb)\b", cleaned))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not series:
        missing.append("series")
    if not ram_type:
        missing.append("ram_type")
    if not total_capacity:
        missing.append("total_capacity")
    if not kit_config:
        missing.append("kit_config")
    if not speed:
        missing.append("speed")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "series": series,
        "ram_type": ram_type,
        "total_capacity": total_capacity,
        "kit_config": kit_config,
        "speed": speed,
        "cas_latency": cas_latency,
        "white": white,
        "rgb": rgb,
    }
    return ExtractionResult(
        category="ram",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

