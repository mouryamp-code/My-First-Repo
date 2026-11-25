"""CPU attribute extractor (`Architecture.md` §5.3).

Required fields:
- `brand`: `amd` | `intel`
- `model`: family + model with suffix (`ryzen-5-7600x`, `ryzen-7-7800x3d`, `core-i5-14400f`, `core-ultra-7-265k`)
- `packaging`: `box` | `tray`

Optional flags:
- `with_cooler`: `bool`
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import ExtractionResult, get_structured_field
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

# AMD Ryzen / Threadripper / Athlon patterns (strictly capturing model suffix: x3d, xt, x, gt, g, ge, f, wx)
_AMD_RYZEN_RE = re.compile(
    r"\bryzen\s+(?:threadripper\s+)?(?P<tier>[3579]|threadripper)?\s*[\-]?\s*"
    r"(?P<num>\d{4})(?P<suffix>x3d2|x3d|xt|x|gt|ge|g|f|wx)?\b",
    re.IGNORECASE,
)
_AMD_ATHLON_RE = re.compile(
    r"\bathlon\s+(?:silver\s+|gold\s+)?(?P<num>\d{3,4})(?P<suffix>ge|g)?\b",
    re.IGNORECASE,
)

# Intel Core i3/i5/i7/i9 and Core Ultra 5/7/9 patterns (strictly capturing suffix: kf, ks, k, f, t, s)
_INTEL_CORE_I_RE = re.compile(
    r"\b(?:core\s+)?(?P<tier>i[3579])[\s\-]+(?P<num>\d{4,5})(?P<suffix>kf|ks|k|f|t)?\b",
    re.IGNORECASE,
)
_INTEL_CORE_ULTRA_RE = re.compile(
    r"\bcore\s+ultra\s+(?P<tier>[579])\s+(?:processor\s+)?(?P<num>\d{3})(?P<suffix>kf|k|f|t)?\b",
    re.IGNORECASE,
)
_INTEL_PENTIUM_CELERON_RE = re.compile(
    r"\b(?P<fam>pentium\s+gold|pentium|celeron)\s+(?P<num>g\d{4,5})(?P<suffix>t)?\b",
    re.IGNORECASE,
)

_TRAY_RE = re.compile(r"\b(?:tray|oem|bulk\s+pack|without\s+box)\b", re.IGNORECASE)
_WITH_COOLER_RE = re.compile(
    r"\bwith\s+(?:wraith(?:\s+stealth|\s+spire|\s+prism)?\s+cooler|stock\s+cooler|cpu\s+cooler|cooler)\b",
    re.IGNORECASE,
)


def _extract_cpu_model(cleaned_title: str, brand_slug: str | None) -> str | None:
    if brand_slug in (None, "amd"):
        m_ryzen = _AMD_RYZEN_RE.search(cleaned_title)
        if m_ryzen:
            tier = (m_ryzen.group("tier") or "").lower()
            num = m_ryzen.group("num").lower()
            suffix = (m_ryzen.group("suffix") or "").lower()
            if "threadripper" in m_ryzen.group(0).lower():
                return f"threadripper-{num}{suffix}"
            if tier:
                return f"ryzen-{tier}-{num}{suffix}"
            return f"ryzen-{num}{suffix}"

        m_athlon = _AMD_ATHLON_RE.search(cleaned_title)
        if m_athlon:
            num = m_athlon.group("num").lower()
            suffix = (m_athlon.group("suffix") or "").lower()
            return f"athlon-{num}{suffix}"

    if brand_slug in (None, "intel"):
        m_ultra = _INTEL_CORE_ULTRA_RE.search(cleaned_title)
        if m_ultra:
            tier = m_ultra.group("tier").lower()
            num = m_ultra.group("num").lower()
            suffix = (m_ultra.group("suffix") or "").lower()
            return f"core-ultra-{tier}-{num}{suffix}"

        m_core = _INTEL_CORE_I_RE.search(cleaned_title)
        if m_core:
            tier = m_core.group("tier").lower()
            num = m_core.group("num").lower()
            suffix = (m_core.group("suffix") or "").lower()
            return f"core-{tier}-{num}{suffix}"

        m_pent = _INTEL_PENTIUM_CELERON_RE.search(cleaned_title)
        if m_pent:
            fam = re.sub(r"\s+", "-", m_pent.group("fam").lower())
            num = m_pent.group("num").lower()
            suffix = (m_pent.group("suffix") or "").lower()
            return f"{fam}-{num}{suffix}"

    return None


def extract_cpu_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized CPU attributes and check for structured field conflicts."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    # Stores write X3D chips with a space ("9800X 3D", "9950X 3D 2"): rejoin them so they are
    # never read as the plain 9800X / 9950X.
    cleaned = re.sub(r"\b(\d{4})x\s*3d(?:\s*(2)\b)?", lambda m: f"{m.group(1)}x3d{m.group(2) or ''}", cleaned)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="cpu", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "cpu", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    model = _extract_cpu_model(cleaned, brand_slug)
    if brand_slug is None and model is not None:
        if model.startswith(("ryzen-", "threadripper-", "athlon-")):
            brand_slug, brand_name = "amd", "AMD"
        elif model.startswith(("core-", "pentium-", "celeron-")):
            brand_slug, brand_name = "intel", "Intel"

    # Determine packaging (`box` vs `tray`)
    is_tray = bool(_TRAY_RE.search(raw_title))
    if mpn:
        if mpn.endswith(("MPK", "TRAY")) or mpn.startswith("CM80"):
            if re.search(r"\b(?:retail\s+box|boxed|box\s+pack)\b", raw_title, re.IGNORECASE):
                conflicts.append(
                    f"Packaging conflict: MPN '{mpn}' is a Tray/OEM part but title states 'Box'"
                )
            is_tray = True
        elif mpn.endswith(("BOX", "WOF", "POF")) or mpn.startswith("BX80"):
            if is_tray:
                conflicts.append(
                    f"Packaging conflict: MPN '{mpn}' is a Retail Box part but title states 'Tray/OEM'"
                )
    sf_pkg = get_structured_field(source_fields, "packaging")
    if sf_pkg:
        sf_pkg_lower = sf_pkg.lower()
        if "tray" in sf_pkg_lower or "oem" in sf_pkg_lower:
            if re.search(r"\b(?:retail\s+box|boxed|box\s+pack)\b", raw_title, re.IGNORECASE):
                conflicts.append(
                    f"Packaging conflict: structured packaging '{sf_pkg}' disagrees with title 'Box'"
                )
            is_tray = True
        elif "box" in sf_pkg_lower and is_tray:
            conflicts.append(
                f"Packaging conflict: structured packaging '{sf_pkg}' disagrees with title 'Tray'"
            )

    packaging = "tray" if is_tray else "box"
    with_cooler = bool(_WITH_COOLER_RE.search(raw_title))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "packaging": packaging,
        "with_cooler": with_cooler,
    }
    return ExtractionResult(
        category="cpu",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

