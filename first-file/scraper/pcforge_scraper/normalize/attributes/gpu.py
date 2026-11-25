"""GPU attribute extractor (`Architecture.md` §5.3).

Required fields:
- `brand`: board partner (`asus`, `msi`, `gigabyte`, `asrock`, `zotac`, `sapphire`, `powercolor`, `inno3d`, `galax`, `colorful`, `pny`, etc.)
- `chipset`: GPU chipset including `Ti` / `Super` / `XT` / `XTX` / `GRE` (`rtx-4060`, `rtx-4060-ti`, `rtx-4070-ti-super`, `rx-7800-xt`, `rx-7900-xtx`)
- `vram`: VRAM capacity (`8gb`, `12gb`, `16gb`, `24gb`, `32gb`, `6gb`, `4gb`, `2gb`)
- `series`: board partner series (`dual`, `tuf`, `rog-strix`, `ventus-2x`, `ventus-3x`, `gaming-x`, `windforce`, `eagle`, `gaming`, `aorus-xtreme`, `aero`, `challenger`, `twin-edge`, `solo`, `pulse`, `nitro-plus`, `pure`, `hellhound`, `red-devil`, `fighter`, etc.)

Optional flags:
- `oc`: `bool`
- `white`: `bool`
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

_NVIDIA_CHIPSET_RE = re.compile(
    r"\b(?P<prefix>rtx|gtx|gt)\s*[\-]?\s*(?P<num>a?\d{3,4})"
    r"(?:\s*[\-]?\s*(?P<s1>ti\s+super|ti|super))?\b",
    re.IGNORECASE,
)
_AMD_CHIPSET_RE = re.compile(
    r"\brx\s*[\-]?\s*(?P<num>\d{3,4})(?:\s*[\-]?\s*(?P<s1>xtx|xt|gre))?\b",
    re.IGNORECASE,
)
_INTEL_ARC_RE = re.compile(
    r"\barc\s*[\-]?\s*(?P<num>[ab]\d{3})\b",
    re.IGNORECASE,
)

# Ordered longest/most-specific first so e.g. `ventus-2x-xs` matches before `ventus-2x`
_GPU_SERIES_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brog[\s\-]+strix\b", re.I), "rog-strix"),
    (re.compile(r"\btuf(?:\s+gaming)?\b", re.I), "tuf"),
    (re.compile(r"\bproart\b", re.I), "proart"),
    (re.compile(r"\basus\b.*\bprime\b", re.I), "prime"),
    (re.compile(r"\bdual\b", re.I), "dual"),
    (re.compile(r"\blp[\s\-]+brk\b", re.I), "lp-brk"),
    (re.compile(r"\bsl\b", re.I), "sl"),
    (re.compile(r"\bsuprim[\s\-]+liquid\b", re.I), "suprim-liquid"),
    (re.compile(r"\bsuprim[\s\-]+x\b|\bsuprim\b", re.I), "suprim"),
    (re.compile(r"\bgaming[\s\-]+x[\s\-]+trio\b", re.I), "gaming-x-trio"),
    (re.compile(r"\bgaming[\s\-]+x[\s\-]+slim\b", re.I), "gaming-x-slim"),
    (re.compile(r"\bgaming[\s\-]+trio\b", re.I), "gaming-trio"),
    (re.compile(r"\bgaming[\s\-]+x\b", re.I), "gaming-x"),
    (re.compile(r"\bventus[\s\-]+3x\b", re.I), "ventus-3x"),
    (re.compile(r"\bventus[\s\-]+2x[\s\-]+xs\b", re.I), "ventus-2x-xs"),
    (re.compile(r"\bventus[\s\-]+2x[\s\-]+e\b", re.I), "ventus-2x-e"),
    (re.compile(r"\bventus[\s\-]+2x\b", re.I), "ventus-2x"),
    (re.compile(r"\bshadow[\s\-]+3x\b", re.I), "shadow-3x"),
    (re.compile(r"\bshadow[\s\-]+2x\b", re.I), "shadow-2x"),
    (re.compile(r"\binspire[\s\-]+3x\b", re.I), "inspire-3x"),
    (re.compile(r"\binspire[\s\-]+2x\b", re.I), "inspire-2x"),
    (re.compile(r"\baorus[\s\-]+xtreme\b|\baorus\b.*\bxtreme\b", re.I), "aorus-xtreme"),
    (re.compile(r"\baorus[\s\-]+master\b", re.I), "aorus-master"),
    (re.compile(r"\baorus[\s\-]+elite\b", re.I), "aorus-elite"),
    (re.compile(r"\bwindforce[\s\-]+2x\b|\bwindforce[\s\-]+3x\b|\bwindforce\b", re.I), "windforce"),
    (re.compile(r"\beagle\b", re.I), "eagle"),
    (re.compile(r"\baero\b", re.I), "aero"),
    (re.compile(r"\bphantom[\s\-]+gaming\b", re.I), "phantom-gaming"),
    (re.compile(r"\bsteel[\s\-]+legend\b", re.I), "steel-legend"),
    (re.compile(r"\btaichi\b", re.I), "taichi"),
    (re.compile(r"\bchallenger\b", re.I), "challenger"),
    (re.compile(r"\bbulk[\s\-]+pack\b", re.I), "bulk-pack"),
    (re.compile(r"\bamp[\s\-]+extreme\b", re.I), "amp-extreme"),
    (re.compile(r"\btwin[\s\-]+edge\b", re.I), "twin-edge"),
    (re.compile(r"\btrinity\b", re.I), "trinity"),
    (re.compile(r"\bsolo\b", re.I), "solo"),
    (re.compile(r"\bamp\b", re.I), "amp"),
    (re.compile(r"\bnitro[\s\-]*plus\b|\bnitro\+", re.I), "nitro-plus"),
    (re.compile(r"\bpulse\b", re.I), "pulse"),
    (re.compile(r"\bpure\b", re.I), "pure"),
    (re.compile(r"\bred[\s\-]+devil\b", re.I), "red-devil"),
    (re.compile(r"\bhellhound\b", re.I), "hellhound"),
    (re.compile(r"\bfighter\b", re.I), "fighter"),
    (re.compile(r"\breaper\b", re.I), "reaper"),
    (re.compile(r"\bbattle[\s\-]+ax\b.*\bnb[\s\-]+duo\b|\bnb[\s\-]+duo\b", re.I), "nb-duo"),
    (re.compile(r"\bbattle[\s\-]+ax\b.*\bnb[\s\-]+ex\b|\bnb[\s\-]+ex\b", re.I), "nb-ex"),
    (re.compile(r"\bigame[\s\-]+ultra\b", re.I), "igame-ultra"),
    (re.compile(r"\bigame[\s\-]+vulcan\b", re.I), "igame-vulcan"),
    (re.compile(r"\bigame[\s\-]+advanced\b", re.I), "igame-advanced"),
    (re.compile(r"\bbattle[\s\-]+ax\b", re.I), "battle-ax"),
    (re.compile(r"\bichill[\s\-]+x4\b", re.I), "ichill-x4"),
    (re.compile(r"\bichill[\s\-]+x3\b", re.I), "ichill-x3"),
    (re.compile(r"\btwin[\s\-]+x2\b", re.I), "twin-x2"),
    (re.compile(r"\b1[\s\-]+click\b", re.I), "1-click"),
    (re.compile(r"\bex[\s\-]+gamer\b", re.I), "ex-gamer"),
    (re.compile(r"\bverto\b", re.I), "verto"),
    (re.compile(r"\bxlr8\b", re.I), "xlr8"),
    (re.compile(r"\bquadro\b", re.I), "quadro"),
    (re.compile(r"\bnforce\b", re.I), "nforce"),
    (re.compile(r"\bfounders[\s\-]+edition\b", re.I), "founders-edition"),
    # Added from live PrimeABGB / PC Studio / SCL / TPS / Vishal data (2026-11).
    (re.compile(r"\bsolid[\s\-]+core\b", re.I), "solid-core"),
    (re.compile(r"\bsolid[\s\-]+sff\b", re.I), "solid-sff"),
    (re.compile(r"\bsolid\b", re.I), "solid"),
    (re.compile(r"\bastral\b", re.I), "astral"),
    (re.compile(r"\blightning\b", re.I), "lightning"),
    (re.compile(r"\bvanguard\b", re.I), "vanguard"),
    (re.compile(r"\bexpert\b", re.I), "expert"),
    (re.compile(r"\bichill[\s\-]+frostbite\b|\bfrostbite\b", re.I), "ichill-frostbite"),
    (re.compile(r"\binno3d\b.*\bx3\b", re.I), "x3"),
    (re.compile(r"\baorus\b.*\bstealth\b", re.I), "aorus-stealth"),
    (re.compile(r"\baorus\b.*\bmaster\b", re.I), "aorus-master"),
    (re.compile(r"\basus\b.*\bt1\b", re.I), "t1"),
    (re.compile(r"\beco\b", re.I), "eco"),
    (re.compile(r"\b(?:lp|low[\s\-]*profile)\b", re.I), "low-profile"),
    (re.compile(r"\bmsi\b.*\bgaming\b", re.I), "gaming"),
    # Gigabyte `Gaming OC` / `Gaming` (matched after specific series like TUF Gaming / Zotac Gaming / Phantom Gaming)
    (re.compile(r"\bgigabyte\b.*\bgaming\b", re.I), "gaming"),
)

# Chipsets that only ship in one VRAM size, so a title without "12GB" can still be keyed.
_SINGLE_VRAM_CHIPSETS: dict[str, str] = {
    "rtx-5090": "32gb",
    "rtx-5080": "16gb",
    "rtx-5070-ti": "16gb",
    "rtx-5070": "12gb",
    "rtx-5060": "8gb",
    "rtx-5050": "8gb",
    "rtx-4090": "24gb",
    "rtx-4080-super": "16gb",
    "rtx-4080": "16gb",
    "rtx-4070-ti-super": "16gb",
    "rtx-4070-ti": "12gb",
    "rtx-4070-super": "12gb",
    "rtx-4070": "12gb",
    "rtx-4060": "8gb",
    "rx-9070-xt": "16gb",
    "rx-9070": "16gb",
    "rx-7900-xtx": "24gb",
    "rx-7900-xt": "20gb",
    "rx-7900-gre": "16gb",
    "rx-7800-xt": "16gb",
    "rx-7700-xt": "12gb",
    "rx-7600-xt": "16gb",
    "rx-7600": "8gb",
}

_VRAM_RE = re.compile(r"\b(2|3|4|6|8|10|11|12|16|20|24|32|48)gb\b", re.IGNORECASE)
_OC_RE = re.compile(r"\b(?:oc|1-click)\b", re.IGNORECASE)
_WHITE_RE = re.compile(r"\b(?:white|ice|snow)\b", re.IGNORECASE)


def _extract_gpu_chipset(cleaned: str) -> str | None:
    m_nv = _NVIDIA_CHIPSET_RE.search(cleaned)
    if m_nv:
        prefix = m_nv.group("prefix").lower()
        num = m_nv.group("num").lower()
        suffix = (m_nv.group("s1") or "").lower().strip()
        suffix_norm = re.sub(r"\s+", "-", suffix)
        return f"{prefix}-{num}-{suffix_norm}" if suffix_norm else f"{prefix}-{num}"

    m_amd = _AMD_CHIPSET_RE.search(cleaned)
    if m_amd:
        num = m_amd.group("num").lower()
        suffix = (m_amd.group("s1") or "").lower().strip()
        return f"rx-{num}-{suffix}" if suffix else f"rx-{num}"

    m_arc = _INTEL_ARC_RE.search(cleaned)
    if m_arc:
        return f"arc-{m_arc.group('num').lower()}"

    return None


# Spec-sheet phrases in long marketplace titles ("| Dual Fan |", "3X Windforce Fans",
# "Dual BIOS", "Dual Ball Fan Bearings") describe cooling hardware, not the product line.
# Left in, they masquerade as the series ("dual", "windforce") and break cross-store matches.
_GPU_SERIES_NOISE_RE = re.compile(
    r"\b(?:dual|twin|triple|tri|single|[1-4]\s*x)[\s\-]*(?:ball[\s\-]+)?(?:fans?|bios|bearings?|slots?)\b"
    r"|\b(?:[1-4]\s*x[\s\-]*)?windforce[\s\-]+(?:fans?|cooling(?:\s+system)?)\b"
    r"|\bfan\s+bearings?\b",
    re.IGNORECASE,
)


def _extract_gpu_series(cleaned: str, brand_slug: str | None) -> str | None:
    cleaned = _GPU_SERIES_NOISE_RE.sub(" ", cleaned)
    for pat, series_slug in _GPU_SERIES_PATTERNS:
        if pat.search(cleaned):
            # Append `-v2` if explicitly marked V2 on Windforce / Ventus / Dual
            if series_slug in ("windforce", "dual", "nb-duo") and re.search(r"\bv2\b", cleaned):
                return f"{series_slug}-v2"
            return series_slug
    # PNY standard non-subseries cards (e.g. "PNY RTX 5050 8GB GDDR6")
    if brand_slug in ("pny", "nextron"):
        return "standard"
    # Entry GT cards (GT 710 / 730 / 1030) are sold without a series name.
    if re.search(r"\bgt[\s\-]?(?:710|730|1030)\b", cleaned):
        return "standard"
    return None


def extract_gpu_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized GPU attributes and check for structured field conflicts."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    # The shared title normalizer turns "Ventus 2X 8GB" into the RAM-kit form "2x8gb". GPUs never
    # come as kits, so split it back: otherwise the series ("ventus 2x") is lost.
    cleaned = re.sub(r"\b(\d)x(\d{1,2}gb)\b", r"\1x \2", cleaned)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="gpu", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "gpu", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    chipset = _extract_gpu_chipset(cleaned)
    m_vram = _VRAM_RE.search(cleaned)
    vram = f"{m_vram.group(1).lower()}gb" if m_vram else None
    if vram is None and chipset in _SINGLE_VRAM_CHIPSETS:
        # Chips sold in exactly one memory size ("RTX 5070 iCHILL X3" is always 12GB).
        vram = _SINGLE_VRAM_CHIPSETS[chipset]

    cap_conflict = check_capacity_conflict(vram, source_fields, field_label="VRAM")
    if cap_conflict:
        conflicts.append(cap_conflict)

    # Check MPN VRAM consistency (e.g. Gigabyte `-8GD` / `-16GD` or ASUS `-O8G` / `-O16G`)
    if mpn and vram:
        m_mpn_vram = re.search(r"(?:-O?(\d{1,2})G(?:D|-|$)|-(\d{1,2})GB)", mpn)
        if m_mpn_vram:
            mpn_gb = f"{int(m_mpn_vram.group(1) or m_mpn_vram.group(2))}gb"
            if mpn_gb != vram:
                conflicts.append(
                    f"MPN VRAM conflict: MPN '{mpn}' indicates '{mpn_gb}' but title states '{vram}'"
                )

    series = _extract_gpu_series(cleaned, brand_slug)
    oc = bool(_OC_RE.search(cleaned))
    if mpn and re.search(r"(?:-OC-|-O\d{1,2}G)", mpn):
        oc = True

    white = bool(_WHITE_RE.search(cleaned)) or series == "pure"

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not chipset:
        missing.append("chipset")
    if not vram:
        missing.append("vram")
    if not series:
        missing.append("series")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "chipset": chipset,
        "vram": vram,
        "series": series,
        "oc": oc,
        "white": white,
    }
    return ExtractionResult(
        category="gpu",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

