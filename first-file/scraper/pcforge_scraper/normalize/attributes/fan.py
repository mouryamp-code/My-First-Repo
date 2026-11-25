"""Case fan attribute extractor."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import RetailerSlug
from pcforge_scraper.normalize.attributes.base import (
    ExtractionResult,
    check_colour_conflict,
)
from pcforge_scraper.normalize.attributes.generic import append_variants, fallback_model
from pcforge_scraper.normalize.brands import detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

# 240/280/360/420 are single-frame multi-fan units (NZXT F360, Karnage Sunbeam 360).
_FAN_SIZE_RE = re.compile(r"\b(80|92|120|140|200|240|280|360|420)\s*mm\b", re.IGNORECASE)
_FAN_PACK_RE = re.compile(
    r"\b(?:(\d)\s*[\-\s]*pack|pack\s+of\s+(\d)|(?:value\s+)?pack\s+(\d)|(\d)\s*in\s*1"
    r"|(\d)\s*(?:x\s*)?fans|x\s*(\d)\b)",
    re.IGNORECASE,
)
# Variants of one fan line that are different products ("SL-INF Flex" vs "SL-INF LCD Flex").
_FAN_VARIANTS: tuple[tuple[str, str], ...] = (
    (r"\blcd\b", "lcd"),
    (r"\bflex\b", "flex"),
    (r"\bwireless\b", "wireless"),
    (r"\brevers(?:e|ed)\b", "reverse"),
    (r"\bmax\b", "max"),
    (r"\bpro\b", "pro"),
    (r"\bv2\b", "v2"),
    (r"\bduo\b", "duo"),
    (r"\bpst\b", "pst"),
    (r"\bippc\b|\bindustrial\s*ppc\b|\bindustrialppc\b", "ippc"),
    (r"\b(?:ippc|industrial\s*ppc|industrialppc)[\s\-]?2000\b", "2k"),
    (r"\b(?:ippc|industrial\s*ppc|industrialppc)[\s\-]?3000\b", "3k"),
    (r"\b5v\b", "5v"),
    (r"\bchromax\b", "chromax"),
    (r"\bredux\b", "redux"),
    (r"\bip67\b", "ip67"),
)

_FAN_PACK_WORDS: dict[str, str] = {
    "single": "1-pack",
    "one": "1-pack",
    "dual": "2-pack",
    "twin": "2-pack",
    "two": "2-pack",
    "triple": "3-pack",
    "three": "3-pack",
    "four": "4-pack",
    "five": "5-pack",
}
_FAN_PACK_WORD_RE = re.compile(
    r"\b(single|one|dual|twin|two|triple|three|four|five)\s+(?:fan\s+)?pack\b", re.IGNORECASE
)
# "3-Fan Pack", "3 fan kit", "3x 120mm", "Pack of 3 fans".
_FAN_PACK_EXTRA_RE = re.compile(
    r"\b(\d)\s*[\-\s]?\s*fans?\s+(?:pack|kit)\b|\b(\d)\s*x\s*1[24]0\s*mm\b", re.IGNORECASE
)
# Single-frame units are one product however many fans they hold.
_SINGLE_FRAME_RE = re.compile(r"\bsingle[\s\-]*frame\b|\bfan\s+unit\b", re.IGNORECASE)

_FAN_MODEL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bp12\s*pwm\s*pst\b", re.I), "p12-pwm-pst"),
    (re.compile(r"\bp12\s*max\b", re.I), "p12-max"),
    (re.compile(r"\bp12\b", re.I), "p12"),
    (re.compile(r"\bp14\s*pwm\s*pst\b", re.I), "p14-pwm-pst"),
    (re.compile(r"\bp14\s*max\b", re.I), "p14-max"),
    (re.compile(r"\bp14\b", re.I), "p14"),
    (re.compile(r"\buni\s*fan\s*sl\s*wireless\s*lcd\b", re.I), "uni-fan-sl-wireless-lcd"),
    (re.compile(r"\buni\s*fan\s*sl\s*wireless\b", re.I), "uni-fan-sl-wireless"),
    (re.compile(r"\buni\s*fan\s*sl[\s\-]*infinity\b|\bsl[\s\-]*inf\b", re.I), "uni-fan-sl-infinity"),
    (re.compile(r"\buni\s*fan\s*tl\s*lcd\b", re.I), "uni-fan-tl-lcd"),
    (re.compile(r"\buni\s*fan\s*tl\b", re.I), "uni-fan-tl"),
    (re.compile(r"\b(?:uni\s*fan\s*)?sl\s*-?\s*(?:120|140)?\s*v2\b", re.I), "uni-fan-sl-v2"),
    (re.compile(r"\buni\s*fan\s*sl\s*-?\s*(?:120|140)\b", re.I), "uni-fan-sl"),
    (re.compile(r"\bfc120\b", re.I), "fc120"),
    (re.compile(r"\brf120\b", re.I), "rf120"),
    (re.compile(r"\bcf120\b", re.I), "cf120"),
    (re.compile(r"\bfk120\b", re.I), "fk120"),
    (re.compile(r"\bsickleflow\s*120\b", re.I), "sickleflow-120"),
    (re.compile(r"\bsickleflow\s*140\b", re.I), "sickleflow-140"),
    (re.compile(r"\bmasterfan\s*mf120\s*halo\b", re.I), "masterfan-mf120-halo"),
    (re.compile(r"\brs120\b", re.I), "rs120"),
    (re.compile(r"\baf120\b", re.I), "af120"),
    (re.compile(r"\bsp120\b", re.I), "sp120"),
    (re.compile(r"\bll120\b", re.I), "ll120"),
    (re.compile(r"\bnf[\s\-]*a12x25\b", re.I), "nf-a12x25"),
    (re.compile(r"\bnf[\s\-]*p12\b", re.I), "nf-p12"),
    (re.compile(r"\bnf[\s\-]*a14\b", re.I), "nf-a14"),
    (re.compile(r"\bf120\s*rgb\s*core\b", re.I), "f120-rgb-core"),
    (re.compile(r"\bf120\b", re.I), "f120"),
    (re.compile(r"\bf140\b", re.I), "f140"),
)

_FALLBACK_FAN_TOKEN_RE = re.compile(r"\b([a-z]{1,4}[\-]?\d{2,3}[a-z0-9\-]*)\b", re.I)


def extract_fan_attributes(
    raw_title: str,
    *,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract normalized Case Fan attributes (`brand`, `model`, `size_mm`, `pack_count`, `rgb`, `white`)."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category="fan", source_fields=source_fields
    )
    mpn = extract_canonical_mpn(raw_mpn, raw_title, "fan", source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)

    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    m_size = _FAN_SIZE_RE.search(cleaned)
    size_mm = f"{m_size.group(1)}mm" if m_size else None

    model: str | None = None
    for pat, slug in _FAN_MODEL_PATTERNS:
        if pat.search(cleaned):
            model = slug
            break
    if model is None:
        for m_tok in _FALLBACK_FAN_TOKEN_RE.finditer(cleaned):
            tok = m_tok.group(1).lower()
            if not tok.endswith("mm") and tok not in ("3in1", "5in1"):
                model = tok
                break
    if model is None and brand_slug:
        model = fallback_model(
            cleaned,
            brand_slug,
            frozenset("cabinet case cpu fan fans cooling pwm pack kit single dual triple frame reverse blade".split()),
        )

    if model:
        model = append_variants(cleaned, model, _FAN_VARIANTS)

    if size_mm is None:
        # Size embedded in a model token: "SL120", "AF140", "NF-A12x25", "P14", "F360".
        m_embedded = re.search(r"[a-z][\-]?(1[24])0?(?:x\d+)?\b", cleaned)
        m_frame = re.search(r"\b(?:f|sunbeam\s*)(240|280|360|420)\b", cleaned)
        if m_frame:
            size_mm = f"{m_frame.group(1)}mm"
        elif m_embedded:
            size_mm = f"{m_embedded.group(1)}0mm"
        elif model and re.search(r"(?:^|-)(120|140)(?:-|$)", model):
            size_mm = f"{re.search(r'(120|140)', model).group(1)}mm"  # type: ignore[union-attr]

    # A multi-pack must never share a key with a single fan: if the title says "pack"
    # but no count can be read, leave pack_count unknown so the listing goes to REVIEW.
    pack_count: str | None = None
    m_word = _FAN_PACK_WORD_RE.search(cleaned)
    m_pack = _FAN_PACK_RE.search(cleaned)
    m_extra = _FAN_PACK_EXTRA_RE.search(cleaned)
    if _SINGLE_FRAME_RE.search(cleaned):
        pack_count = "1-pack"
    elif m_word:
        pack_count = _FAN_PACK_WORDS[m_word.group(1).lower()]
    elif m_extra:
        pack_count = f"{int(m_extra.group(1) or m_extra.group(2))}-pack"
    elif m_pack:
        num = next(g for g in m_pack.groups() if g)
        pack_count = f"{int(num)}-pack"
    elif not re.search(r"\b(?:pack|kit|combo)\b", cleaned):
        pack_count = "1-pack"

    rgb = bool(re.search(r"\b(?:argb|a-rgb|rgb)\b", cleaned))
    white = bool(re.search(r"\b(?:white|wh)\b", cleaned))

    missing: list[str] = []
    if not brand_slug:
        missing.append("brand")
    if not model:
        missing.append("model")
    if not size_mm:
        missing.append("size_mm")
    if not pack_count:
        missing.append("pack_count")

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "size_mm": size_mm,
        "pack_count": pack_count,
        "rgb": rgb,
        "white": white,
    }
    return ExtractionResult(
        category="fan",
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )

