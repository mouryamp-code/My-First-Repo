"""PSU attribute extractor (`Architecture.md` §5.3).

Required fields:
- `brand`: `corsair`, `cooler-master`, `deepcool`, `msi`, `ant-esports`, `antec`, `thermaltake`, `seasonic`, `silverstone`, `xpg`, etc.
- `model`: PSU series/model (`rm850e`, `rm850x`, `mwe-750-v2`, `pn850d`, `pn850m`, `mag-a850gl`, `fg850-v2`, `rx650`, `vs550l`)
- `wattage`: `450w`, `550w`, `650w`, `750w`, `850w`, `1000w`, etc.
- `efficiency`: `white` | `bronze` | `silver` | `gold` | `platinum` | `titanium`
- `modularity`: `full-modular` | `semi-modular` | `non-modular`

Optional flags:
- `atx3`: `bool`
- `white`: `bool`
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import ExtractionResult, check_colour_conflict
from pcforge_scraper.normalize.attributes.generic import fallback_model, trailing_versions
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

_WATTAGE_RE = re.compile(r"\b(3[5-9]0|[4-9][05]0|[12]\d[05]0|3000)\s*w(?:att|atts|t)?\b")
_MODEL_WATTAGE_RE = re.compile(
    r"\b(?:rm|cx|cv|hx|ax|tx|sf|pn|pk|pl|pq|px|dq|fg|rx|vs|ag|mag\s+a|mwe\s+)"
    r"(400|450|500|550|600|650|750|850|1000|1050|1200|1300|1500|1600)[a-z]{0,2}\b",
    re.IGNORECASE,
)

_EFFICIENCY_RE = re.compile(
    r"\b(?:80plus\s+)?(?P<eff>titanium|platinum|gold|silver|bronze)\b|\b80plus\s+(?P<w_eff>white|standard)\b",
    re.IGNORECASE,
)

_ATX3_RE = re.compile(
    r"\b(?:atx\s*3(?:\.[01])?|atx3(?:\.[01])?|pcie\s*5(?:\.[01])?|pcie5)\b",
    re.IGNORECASE,
)

_PSU_MODEL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Corsair RM850e, RM850x, RM750e, RM1000e, CX650, CV550, HX1000i, SF750
    (re.compile(r"\b((?:rm|cx|cv|hx|ax|tx|sf)\d{3,4}[exi]?)\b", re.I), "{0}"),
    # MSI MAG A850GL, MAG A750GL, MAG A650BN, MPG A850G
    (re.compile(r"\b((?:mag|mpg|meg)[\s\-]+a\d{3,4}[a-z]{1,3})\b", re.I), "{0}"),
    # DeepCool PN850D, PN850M, PN750D, PN750M, PK650D, PL650D, PQ850M, PX1000G
    (re.compile(r"\b((?:pn|pk|pl|pq|px|dq|pm)\d{3,4}[a-z]{1,2})\b", re.I), "{0}"),
    # Cooler Master MWE 750W V2 / MWE Gold 850 V2 / MWE Bronze 650 V3
    (
        re.compile(r"\bmwe(?:\s+(?:gold|bronze|white))?\s+(\d{3,4})w?(?:\s+(v[23]))?\b", re.I),
        "mwe-{0}-{1}",
    ),
    # Ant Esports FG550 V2, FG650 V2, FG850 V2, FG1000 V2, RX550, RX650, RX750, RX850, VS450L, VS550L, VS650L
    (re.compile(r"\b(fg\d{3,4})(?:\s+(v2))?\b", re.I), "{0}-{1}"),
    (re.compile(r"\b((?:rx|vs|fp)\d{3,4}l?)\b", re.I), "{0}"),
    # Antec Atom V450 V2, Atom V550 V2, NeoECO, CSK, HCG
    (re.compile(r"\b(atom[\s\-]+[vbg]\d{3}(?:[\s\-]+v2)?)\b", re.I), "{0}"),
    # Thermaltake Litepower 450W, Toughpower GF3 850W, Smart BX1
    (re.compile(r"\b(litepower|toughpower(?:\s+gf[13a])?|smart(?:\s+bx1|\s+bm2)?)\b", re.I), "{0}"),
    # XPG Core Reactor II 850W, Kyber 750W, Pylon 650W
    (re.compile(r"\b(core[\s\-]+reactor(?:\s+ii)?|kyber|pylon)\b", re.I), "{0}"),
    # Seasonic Focus GX-850, Vertex GX-1000
    (re.compile(r"\b((?:focus|vertex|prime)[\s\-]+gx[\s\-]?\d{3,4})\b", re.I), "{0}"),
)


_PSU_STOP_WORDS = frozenset(
    """
    power supply supplies smps psu unit watt watts atx modular full-modular semi-modular non-modular
    fully full semi non 80plus 80 plus bronze silver gold platinum titanium cybenetics compliant
    pcie native cable cables fan quiet silent low noise version support ready mm sfx sfx-l
    """.split()
)


def _extract_psu_model(cleaned: str) -> str | None:
    for pat, template in _PSU_MODEL_PATTERNS:
        m = pat.search(cleaned)
        if m:
            groups = [re.sub(r"\s+", "-", (g or "").lower().strip()) for g in m.groups()]
            if template == "mwe-{0}-{1}":
                return f"mwe-{groups[0]}-{groups[1]}" if groups[1] else f"mwe-{groups[0]}"
            if template == "{0}-{1}":
                return f"{groups[0]}-{groups[1]}" if groups[1] else groups[0]
            return groups[0]
    return None


def extract_psu_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized PSU attributes and check for structured field conflicts."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="psu", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "psu", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    model = _extract_psu_model(cleaned)
    if model is None and brand_slug:
        model = fallback_model(cleaned, brand_slug, _PSU_STOP_WORDS)
    if model:
        # "MAG A750GL PCIE5 II" is a different unit from "MAG A750GL PCIE5".
        model = trailing_versions(cleaned, model)

    m_watt = _WATTAGE_RE.search(cleaned)
    wattage: str | None = f"{m_watt.group(1)}w" if m_watt else None
    if wattage is None:
        m_mw = _MODEL_WATTAGE_RE.search(cleaned)
        if m_mw:
            wattage = f"{m_mw.group(1)}w"

    m_eff = _EFFICIENCY_RE.search(cleaned)
    efficiency: str | None = None
    if m_eff:
        if m_eff.group("eff"):
            efficiency = m_eff.group("eff").lower()
        elif m_eff.group("w_eff"):
            efficiency = "white"

    modularity: str | None = None
    if "full-modular" in cleaned:
        modularity = "full-modular"
    elif "semi-modular" in cleaned:
        modularity = "semi-modular"
    elif "non-modular" in cleaned:
        modularity = "non-modular"

    atx3 = bool(_ATX3_RE.search(cleaned))

    # Strip "80plus white" before checking whether PSU casing colour is White
    cleaned_no_eff_white = re.sub(r"\b80plus\s+white\b", "", cleaned)
    white = bool(re.search(r"\b(?:white|snow)\b", cleaned_no_eff_white))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")
    if not wattage:
        missing.append("wattage")
    if not efficiency:
        missing.append("efficiency")
    if not modularity:
        missing.append("modularity")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "wattage": wattage,
        "efficiency": efficiency,
        "modularity": modularity,
        "atx3": atx3,
        "white": white,
    }
    return ExtractionResult(
        category="psu",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

