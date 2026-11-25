"""Generic model-name fallback shared by component extractors.

Each category extractor first tries its curated pattern list (the most reliable source). When no
pattern matches, `fallback_model` reads the model straight from the title:

1. the first part-code token (`c850`, `ag276qzd2`, `nm790`, `l360aio5`) plus trailing version
   words (`v2`, `pro`, `iii`), else
2. the first few product-line words after the brand (`elite`, `liquid freezer iii pro`,
   `phantom spirit 120`), stopping at the first descriptive or spec word.

Store SKUs and manufacturer part numbers (`ZT-B50800J2S-10P`, `920-012726`) are never used as a
model, because most stores leave them out and the listing would then never match across stores.
"""

from __future__ import annotations

import re

from pcforge_scraper.normalize.brands import BRAND_BY_SLUG

_COMMON_STOP = frozenset(
    """
    for with and or the a of in to by from new latest original genuine official edition series
    black white grey gray silver red blue green pink purple gold snow frost
    rgb argb led
    pc computer desktop gaming gamer internal external
    """.split()
)

_VERSION_WORDS = frozenset("v2 v3 v4 v5 ii iii iv pro plus max mini lite ultra evo se x s".split())

# Specs and units that can look like model codes.
_SPEC_RE = re.compile(
    r"^(?:\d+(?:w|watt|watts|gb|tb|mb|mhz|hz|mm|cm|cms|in|inch|mbps|rpm|k|p|ms)|"
    r"\d+(?:\.\d+)?|pcie\d(?:\.\d)?|gen\d|atx\d(?:\.\d)?|ddr\d|gddr\d|m\.?2|80plus|"
    r"usb\d?|nvme|sata\d?|4k|2k|5k|8k|fhd|qhd|uhd|wqhd|hdr\d*|am\d|lga\d+)$"
)


def _is_part_number(token: str) -> bool:
    return sum(c.isdigit() for c in token) >= 5 or token.count("-") >= 2


def _brand_words(brand_slug: str | None) -> set[str]:
    entry = BRAND_BY_SLUG.get(brand_slug or "")
    words: set[str] = set()
    if entry:
        for alias in entry.aliases:
            words.update(alias.lower().replace("-", " ").split())
        words.update(entry.name.lower().replace("-", " ").split())
    # Aliases like "samsung 990" must not turn "990" into a brand word.
    return {w for w in words if not any(c.isdigit() for c in w)}


def fallback_model(
    cleaned: str,
    brand_slug: str | None,
    stop_words: frozenset[str] | set[str] = frozenset(),
    *,
    max_words: int = 3,
    allow_numbers: bool = False,
) -> str | None:
    """Best-effort model slug from a normalized title, or `None` if nothing model-like is left.

    `allow_numbers` keeps bare numbers that are part of a product name ("Phantom Spirit 120",
    "Samsung 990") instead of treating them as specs.
    """
    brand_words = _brand_words(brand_slug)
    stop = _COMMON_STOP | set(stop_words)
    tokens = []
    for raw in re.findall(r"[a-z0-9][a-z0-9\-\.\+]*", cleaned):
        tok = raw.rstrip(".-+")
        if not tok or tok in brand_words or _is_part_number(tok):
            continue
        tokens.append(tok)

    # 1) part code (letters + digits) and its version words
    for i, tok in enumerate(tokens):
        if (
            re.search(r"[a-z]", tok)
            and re.search(r"\d", tok)
            and len(tok) >= 3
            and not _SPEC_RE.match(tok)
            and tok not in stop
        ):
            parts = [tok]
            for nxt in tokens[i + 1 : i + 3]:
                if nxt in _VERSION_WORDS:
                    parts.append(nxt)
                else:
                    break
            return "-".join(parts)

    # 2) leading product-line words, then any version words right after them
    words: list[str] = []
    rest: list[str] = []
    for i, tok in enumerate(tokens):
        is_number = tok.isdigit() and 2 <= len(tok) <= 4
        if tok in stop or (_SPEC_RE.match(tok) and not (allow_numbers and is_number)):
            if words:
                rest = tokens[i:]
                break
            continue
        words.append(tok)
        if len(words) >= max_words:
            rest = tokens[i + 1 :]
            break
    for tok in rest:
        if tok in _VERSION_WORDS and tok not in words:
            words.append(tok)
        else:
            break
    return "-".join(words) if words else None


def trailing_versions(cleaned: str, model: str) -> str:
    """Append generation markers found anywhere in the title ("ROG Thor 1200W Platinum III")."""
    found = [v for v in re.findall(r"\b(ii|iii|iv|v2|v3|v4|v5|evo)\b", cleaned) if v not in model.split("-")]
    return "-".join([model, *dict.fromkeys(found)]) if found else model


def append_variants(cleaned: str, model: str, variants: tuple[tuple[str, str], ...]) -> str:
    """Append variant markers found in the title, in a fixed order, so every store agrees.

    `variants` is `(regex, slug)` pairs, e.g. `(r"\\bzero\\s*dark\\b", "zero-dark")`. A variant
    already part of the model is not added twice.
    """
    parts = model.split("-")
    extra = []
    for pattern, slug in variants:
        if re.search(pattern, cleaned) and slug not in model and slug not in extra:
            if all(piece in parts for piece in slug.split("-")):
                continue
            extra.append(slug)
    return "-".join([model, *extra]) if extra else model
