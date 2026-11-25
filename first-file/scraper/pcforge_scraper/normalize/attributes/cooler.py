"""CPU cooler (Air & AIO Liquid Cooler) attribute extractor."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import (
    ExtractionResult,
    check_colour_conflict,
)
from pcforge_scraper.normalize.attributes.generic import (
    append_variants,
    fallback_model,
)
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

_COOLER_MODEL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bak620\s*digital\b", re.I), "ak620-digital"),
    (re.compile(r"\bak620\b", re.I), "ak620"),
    (re.compile(r"\bak500\b", re.I), "ak500"),
    (re.compile(r"\bak400\s*digital\b", re.I), "ak400-digital"),
    (re.compile(r"\bak400\b", re.I), "ak400"),
    (re.compile(r"\bag620\b", re.I), "ag620"),
    (re.compile(r"\bag400\b", re.I), "ag400"),
    (re.compile(r"\ble720\b", re.I), "le720"),
    (re.compile(r"\ble520\b", re.I), "le520"),
    (re.compile(r"\ble500\b", re.I), "le500"),
    (re.compile(r"\bls720\b", re.I), "ls720"),
    (re.compile(r"\bls520\b", re.I), "ls520"),
    (re.compile(r"\blt720\b", re.I), "lt720"),
    (re.compile(r"\blt520\b", re.I), "lt520"),
    (re.compile(r"\bmystique\s*360\b", re.I), "mystique-360"),
    (re.compile(r"\bmystique\s*240\b", re.I), "mystique-240"),
    (re.compile(r"\bhyper\s*212\s*halo\b", re.I), "hyper-212-halo"),
    (re.compile(r"\bhyper\s*212\s*spectrum\b", re.I), "hyper-212-spectrum"),
    (re.compile(r"\bhyper\s*212\b", re.I), "hyper-212"),
    (re.compile(r"\bhyper\s*620s\b", re.I), "hyper-620s"),
    (re.compile(r"\bmasterliquid\s*360l\s*core\b", re.I), "masterliquid-360l-core"),
    (re.compile(r"\bmasterliquid\s*240l\s*core\b", re.I), "masterliquid-240l-core"),
    (re.compile(r"\bmasterliquid\s*360\s*atmos\b", re.I), "masterliquid-360-atmos"),
    (re.compile(r"\bmasterliquid\s*ml240l\b", re.I), "masterliquid-ml240l"),
    (re.compile(r"\bmasterliquid\s*ml360l\b", re.I), "masterliquid-ml360l"),
    (re.compile(r"\bliquid\s*freezer\s*iii\s*360\b", re.I), "liquid-freezer-iii-360"),
    (re.compile(r"\bliquid\s*freezer\s*iii\s*240\b", re.I), "liquid-freezer-iii-240"),
    (re.compile(r"\bliquid\s*freezer\s*ii\s*360\b", re.I), "liquid-freezer-ii-360"),
    (re.compile(r"\bliquid\s*freezer\s*ii\s*240\b", re.I), "liquid-freezer-ii-240"),
    (re.compile(r"\bfreezer\s*36\b", re.I), "freezer-36"),
    (re.compile(r"\bnh[\s\-]*d15\b", re.I), "nh-d15"),
    (re.compile(r"\bnh[\s\-]*u12a\b", re.I), "nh-u12a"),
    (re.compile(r"\bnh[\s\-]*u12s\b", re.I), "nh-u12s"),
    (re.compile(r"\bgalahad\s*ii\s*trinity\s*360\b", re.I), "galahad-ii-trinity-360"),
    (re.compile(r"\bgalahad\s*ii\s*trinity\s*240\b", re.I), "galahad-ii-trinity-240"),
    (re.compile(r"\bgalahad\s*ii\s*lcd\s*360\b", re.I), "galahad-ii-lcd-360"),
    (re.compile(r"\bkraken\s*elite\s*360\b", re.I), "kraken-elite-360"),
    (re.compile(r"\bkraken\s*360\b", re.I), "kraken-360"),
    (re.compile(r"\bkraken\s*240\b", re.I), "kraken-240"),
    (re.compile(r"\bmag\s*coreliquid\s*e360\b", re.I), "mag-coreliquid-e360"),
    (re.compile(r"\bmag\s*coreliquid\s*e240\b", re.I), "mag-coreliquid-e240"),
    (re.compile(r"\bmag\s*coreliquid\s*m360\b", re.I), "mag-coreliquid-m360"),
    (re.compile(r"\bmag\s*coreliquid\s*m240\b", re.I), "mag-coreliquid-m240"),
    (re.compile(r"\bice\s*400\b|\bice[\s\-]*c612\b|\bice[\s\-]*120\b|\bice[\s\-]*240\b|\bice[\s\-]*360\b", re.I), "ice-series"),
)

_FALLBACK_COOLER_TOKEN_RE = re.compile(r"\b([a-z]{1,4}[\-]?\d{2,4}[a-z0-9\-]*)\b", re.I)


# Variants sold under the same base model ("AK400" vs "AK400 G2 Digital").
_COOLER_VARIANTS: tuple[tuple[str, str], ...] = (
    (r"\bg2\b", "g2"),
    (r"\bzero\s*dark\b", "zero-dark"),
    (r"\bdigital\b", "digital"),
    (r"\bnyx\b", "nyx"),
    (r"\bwood\s*grain\b", "wood-grain"),
    (r"\bzoria\b", "zoria"),
    (r"\bflow\b", "flow"),
    (r"\b3dhp\b", "3dhp"),
    (r"\bspectrum\b", "spectrum"),
    (r"\bvision\b", "vision"),
    (r"\bhbc\b", "hbc"),
    (r"\blbc\b", "lbc"),
    (r"\bchromax\b", "chromax"),
    (r"\blcd\b|\bscreen\b|\bdisplay\b", "lcd"),
    (r"\bultra\b", "ultra"),
    (r"\bex\b", "ex"),
    (r"\bv2\b", "v2"),
    (r"\bv3\b", "v3"),
    (r"\bv4\b", "v4"),
    (r"\bse\b", "se"),
    (r"\bpro\b", "pro"),
    (r"\bplus\b", "plus"),
    (r"\bmax\b", "max"),
    (r"\bevo\b", "evo"),
    # Gigabyte "Aorus 240" vs "Gaming 240"; server-socket editions of desktop coolers.
    (r"\baorus\b", "aorus"),
    (r"\bgaming\b", "gaming"),
    (r"\beagle\b", "eagle"),
    (r"\bdx[\s\-]?(\d{4})\b", "dx"),
    (r"\btr5[\s\-]?sp6\b|\bsp6\b", "tr5-sp6"),
    (r"\btr4\b|\bstr4\b", "tr4"),
    (r"\blga\s?3647\b", "lga3647"),
    (r"\blga\s?4189\b", "lga4189"),
    (r"\bxeon\b", "xeon"),
    (r"\brog\s*strix\b", "rog-strix"),
    (r"\bprime\s+lc\b", "prime"),
)

_COOLER_STOP_WORDS = frozenset(
    """
    cpu cooler coolers cooling air aio all-in-one water heatsink tower single dual fan fans radiator
    mm 120mm 140mm 240mm 280mm 360mm 420mm intel amd socket support compatible with
    """.split()
)


def _extract_cooler_type(cleaned: str) -> str:
    if re.search(r"\b(?:420\s*mm|420)\b", cleaned) and re.search(r"\b(?:liquid|aio|water|radiator)\b", cleaned):
        return "aio-420mm"
    if re.search(r"\b(?:360\s*mm|360|le720|ls720|lt720|e360|m360)\b", cleaned):
        return "aio-360mm"
    if re.search(r"\b(?:280\s*mm|280)\b", cleaned) and re.search(r"\b(?:liquid|aio|water|radiator)\b", cleaned):
        return "aio-280mm"
    if re.search(r"\b(?:240\s*mm|240|le520|le500|ls520|lt520|e240|m240)\b", cleaned):
        return "aio-240mm"
    if re.search(r"\b(?:120\s*mm)\b", cleaned) and re.search(r"\b(?:liquid|aio|water)\b", cleaned):
        return "aio-120mm"
    m_model_size = re.search(r"\b[a-z]{1,4}(240|280|360|420)\b", cleaned)
    if m_model_size and re.search(r"\b(?:liquid|aio|water)\b", cleaned):
        return f"aio-{m_model_size.group(1)}mm"
    return "air"


def extract_cooler_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized CPU cooler attributes (`brand`, `model`, `cooler_type`, `rgb`, `white`)."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="cooler", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "cooler", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    cooler_type = _extract_cooler_type(cleaned)

    model: str | None = None
    for pat, slug in _COOLER_MODEL_PATTERNS:
        if pat.search(cleaned):
            model = slug
            break
    if model is None:
        for m_tok in _FALLBACK_COOLER_TOKEN_RE.finditer(cleaned):
            tok = m_tok.group(1).lower()
            if tok not in ("120mm", "240mm", "360mm", "am4", "am5", "lga1700") and not tok.endswith("mm"):
                model = tok
                break

    if model is None and brand_slug:
        model = fallback_model(cleaned, brand_slug, _COOLER_STOP_WORDS, max_words=4, allow_numbers=True)
    if model:
        model = append_variants(cleaned, model, _COOLER_VARIANTS)

    rgb = bool(re.search(r"\b(?:argb|a-rgb|rgb)\b", cleaned))
    white = bool(re.search(r"\b(?:white|wh|snow)\b", cleaned))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "cooler_type": cooler_type,
        "rgb": rgb,
        "white": white,
    }
    return ExtractionResult(
        category="cooler",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

