"""Motherboard attribute extractor (`Architecture.md` §5.3).

Required fields:
- `brand`: `asus`, `msi`, `gigabyte`, `asrock`, `colorful`, `ant-esports`, etc.
- `model`: chipset + board name (`b650-gaming-plus`, `b650m-gaming-x`, `b550m-pro-vdh`, `h610m-k-v2`)
- `memory_type`: `ddr4` | `ddr5` | `ddr3`
- `form_factor`: `atx` | `matx` | `itx` | `eatx`

Optional flags:
- `wifi`: `bool`
- `white`: `bool`
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import (
    ExtractionResult,
    check_colour_conflict,
    get_structured_field,
)
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

# Captures AMD & Intel chipset token (including trailing M / I / E suffix when attached to chipset)
_MB_CHIPSET_RE = re.compile(
    r"\b(?P<chip>"
    r"(?:a320|b350|b365|b450|x470|a520|b550|x570s?|a620|b650e?|x670e?|b840|b850|x870e?|"
    r"g41|h61|h81|h110|q270|h310|h410|b460|z490|h510|b560|z590|h610|b660|h670|z690|b760|h770|z790|h810|b860|z890)"
    r"(?P<ff_suffix>m|i)?"
    r")(?:(?P<mem_suffix>ad[34])|-|\b)",
    re.IGNORECASE,
)

# Chipsets that physically support ONLY DDR5 (AM5 & LGA1851)
_DDR5_ONLY_CHIPSETS = frozenset(
    {"a620", "b650", "b650e", "x670", "x670e", "b840", "b850", "x870", "x870e", "h810", "b860", "z890"}
)
# Chipsets that physically support ONLY DDR4 (AM4 & LGA1151/1200)
_DDR4_ONLY_CHIPSETS = frozenset(
    {
        "a320",
        "b350",
        "b365",
        "b450",
        "x470",
        "a520",
        "b550",
        "x570",
        "x570s",
        "h110",
        "q270",
        "h310",
        "h410",
        "b460",
        "z490",
        "h510",
        "b560",
        "z590",
    }
)
_DDR3_ONLY_CHIPSETS = frozenset({"g41", "h61", "h81"})

_WIFI_RE = re.compile(r"\b(?:wifi|ax|ac)\b", re.IGNORECASE)
_WHITE_RE = re.compile(r"\b(?:white|ice)\b", re.IGNORECASE)

# Tokens stripped from the board name suffix after the chipset
_MB_STRIP_TOKENS = frozenset(
    {
        "amd",
        "intel",
        "desktop",
        "motherboard",
        "mainboard",
        "ddr3",
        "ddr4",
        "ddr5",
        "d4",
        "d5",
        "atx",
        "matx",
        "itx",
        "eatx",
        "wifi",
        "wifi6",
        "wifi6e",
        "wifi7",
        "ax",
        "ac",
        "am4",
        "am5",
        "lga1700",
        "lga1200",
        "lga1851",
        "socket",
        "usb",
        "3.2",
        "gen2",
        "sata",
        "6gb",
        "s",
        "4.0",
        "5.0",
    }
)


# Words that end the board name: everything after them is a marketplace spec sheet
_MB_SPEC_START_TOKENS = frozenset(
    {
        "motherboard",
        "mainboard",
        "supports",
        "support",
        "compatible",
        "with",
        "ryzen",
        "socket",
        "lga",
        "micro",
        "mini",
        "processor",
        "processors",
        "cpus",
        "chipset",
        "pcie",
    }
)


def _extract_memory_type(raw_title: str, cleaned: str, base_chip: str | None) -> tuple[str | None, str | None]:
    """Return `(memory_type, conflict_note)`."""
    explicit_ddr5 = bool(re.search(r"\b(?:ddr5|d5)\b", cleaned))
    explicit_ddr4 = bool(re.search(r"\b(?:ddr4|d4)\b|ad4\b", cleaned))
    explicit_ddr3 = bool(re.search(r"\b(?:ddr3|d3)\b|ad3\b", cleaned))

    if explicit_ddr5 and explicit_ddr4:
        return None, "Memory type conflict: title mentions both DDR4 and DDR5"

    explicit: str | None = None
    if explicit_ddr5:
        explicit = "ddr5"
    elif explicit_ddr4:
        explicit = "ddr4"
    elif explicit_ddr3:
        explicit = "ddr3"

    if base_chip:
        if base_chip in _DDR5_ONLY_CHIPSETS:
            if explicit and explicit != "ddr5":
                return (
                    explicit,
                    f"Chipset/memory conflict: chipset '{base_chip.upper()}' is DDR5-only but title states '{explicit.upper()}'",
                )
            return "ddr5", None
        if base_chip in _DDR4_ONLY_CHIPSETS:
            if explicit and explicit != "ddr4":
                return (
                    explicit,
                    f"Chipset/memory conflict: chipset '{base_chip.upper()}' is DDR4-only but title states '{explicit.upper()}'",
                )
            return "ddr4", None
        if base_chip in _DDR3_ONLY_CHIPSETS:
            return explicit or "ddr3", None

    # Dual-memory chipsets (H610, B660, B760, Z690, Z790) require explicit DDR4/DDR5 in the title
    return explicit, None


def _extract_form_factor(cleaned: str, chip_token: str | None) -> str | None:
    if re.search(r"\beatx\b", cleaned):
        return "eatx"
    if re.search(r"\bitx\b", cleaned):
        return "itx"
    if re.search(r"\bmatx\b", cleaned):
        return "matx"
    if re.search(r"\batx\b", cleaned):
        return "atx"

    if chip_token:
        c_lower = chip_token.lower()
        if c_lower.endswith("m"):
            return "matx"
        if c_lower.endswith("i"):
            return "itx"
        return "atx"
    return None


def _extract_board_model(cleaned: str, m_chip: re.Match[str], brand_slug: str | None) -> str:
    chip_token = m_chip.group("chip").lower()
    mem_suffix = (m_chip.group("mem_suffix") or "").lower()

    # Check prefix immediately before chipset for ASUS (`tuf`, `rog-strix`, `prime`, `proart`) or MSI (`pro`)
    prefix_slice = cleaned[: m_chip.start()].strip()
    board_prefix = ""
    if brand_slug == "asus":
        if "rog strix" in prefix_slice or "rog-strix" in prefix_slice:
            board_prefix = "rog-strix-"
        elif "tuf" in prefix_slice:
            board_prefix = "tuf-"
        elif "proart" in prefix_slice:
            board_prefix = "proart-"
        elif "prime" in prefix_slice:
            board_prefix = "prime-"
    elif brand_slug == "msi":
        # Keep `pro-` when MSI writes `PRO B650M-A` or `PRO H610M-S`, but drop `mag`/`mpg`/`meg` umbrella tier
        if re.search(r"\bpro\s*$", prefix_slice):
            board_prefix = "pro-"

    after_chip = cleaned[m_chip.end() :].strip()
    if mem_suffix:
        # e.g. "H610MAD4-N" -> "h610m-ad4-n"
        after_chip = f"{mem_suffix} {after_chip}".strip()

    # Split remaining tokens and keep model-identifying tokens
    raw_tokens = re.split(r"[\s\-_.]+", after_chip)
    kept_tokens: list[str] = []
    base_chip = chip_token.rstrip("mi")
    for tok in raw_tokens:
        t = tok.strip().lower()
        if not t or t in _MB_STRIP_TOKENS or not re.search(r"[a-z0-9]", t):
            continue
        if brand_slug and t == brand_slug:
            continue
        # Stop if the chipset is repeated later in the title ("B550M-K ARGB AMD B550 ...")
        if t in (chip_token, base_chip):
            break
        # Stop where marketplace spec text begins ("... Motherboard, Supports AMD Ryzen 7000 ...")
        if t in _MB_SPEC_START_TOKENS:
            break
        kept_tokens.append(t)

    suffix_str = "-".join(kept_tokens)
    if suffix_str:
        return f"{board_prefix}{chip_token}-{suffix_str}"
    return f"{board_prefix}{chip_token}"


def extract_motherboard_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized Motherboard attributes and check for structured field conflicts."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="motherboard", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "motherboard", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    m_chip = _MB_CHIPSET_RE.search(cleaned)
    chip_token: str | None = None
    base_chip: str | None = None
    model: str | None = None

    if m_chip:
        chip_token = m_chip.group("chip").lower()
        ff_suf = (m_chip.group("ff_suffix") or "").lower()
        base_chip = chip_token[: -len(ff_suf)] if ff_suf else chip_token
        model = _extract_board_model(cleaned, m_chip, brand_slug)

    memory_type, mem_conflict = _extract_memory_type(raw_title, cleaned, base_chip)
    if mem_conflict:
        conflicts.append(mem_conflict)

    # Check structured memory_type if provided
    sf_mem = get_structured_field(source_fields, "memory_type", "ram_type")
    if sf_mem and memory_type:
        sf_mem_norm = sf_mem.strip().lower()
        if sf_mem_norm in ("ddr4", "ddr5") and sf_mem_norm != memory_type:
            conflicts.append(
                f"Memory type conflict: structured field '{sf_mem}' disagrees with title '{memory_type.upper()}'"
            )

    form_factor = _extract_form_factor(cleaned, chip_token)
    wifi = bool(_WIFI_RE.search(cleaned))
    white = bool(_WHITE_RE.search(cleaned))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")
    if not memory_type:
        missing.append("memory_type")
    if not form_factor:
        missing.append("form_factor")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "chipset": base_chip,
        "model": model,
        "memory_type": memory_type,
        "form_factor": form_factor,
        "wifi": wifi,
        "white": white,
    }
    return ExtractionResult(
        category="motherboard",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )
