"""Attribute extractors for peripherals: keyboards, mice, headsets and game controllers.

Peripheral titles have no structured spec like a socket or a capacity, so the match key is built
from the brand, a model identifier, and the variant details that change the product:

- connection (wired / wireless),
- colourway (white, plus named colourways such as "Sea Blue" or "Lilac"),
- for keyboards, the switch ("Red", "Star Vector", "Magnetic") and a TKL marker.

The model identifier is:

1. a known flagship name (DualSense, Xbox Wireless Controller, ...), else
2. the first part-code token (`G502`, `K552`, `SC620`) plus trailing version words (`X`, `Plus`,
   `Pro`, `V2`), else
3. the first few product-line words left after removing the brand and descriptive filler
   (`DeathAdder V3 Pro`, `Cloud III`).

Stores describe the same product differently, so a detail one store leaves out simply produces a
separate product rather than a guess. A wrong merge would show one product's price on another;
a missed merge only costs a comparison. Listings with no brand or model go to REVIEW.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pcforge_scraper.config import CategorySlug, RetailerSlug
from pcforge_scraper.normalize.attributes.base import ExtractionResult, check_colour_conflict
from pcforge_scraper.normalize.attributes.generic import append_variants
from pcforge_scraper.normalize.brands import BRAND_BY_SLUG, detect_brand
from pcforge_scraper.normalize.text import extract_canonical_mpn, normalize_title

PERIPHERAL_NOUN: dict[str, str] = {
    "keyboard": "Keyboard",
    "mouse": "Mouse",
    "headset": "Headset",
    "controller": "Controller",
}

# Words that describe a product rather than name it. Model extraction stops at the first one.
_FILLER = frozenset(
    """
    gaming gamer esports e-sports pro-gaming wired wireless bluetooth tri-mode dual-mode triple-mode
    tri dual mode 2.4ghz 2.4g 2.4 ghz usb usb-c type-c rgb argb led backlit backlight lightsync chroma
    hyperspeed lightspeed slipstream quickswitch mechanical membrane optical hot-swappable hotswap hot
    swappable gasket mount low-profile low profile
    keyboard keyboards mouse mice headset headsets headphone headphones earphones earphone
    controller controllers gamepad gamepads joystick over-ear on-ear in-ear surround sound 7.1 virtual
    mic microphone noise cancelling cancellation detachable ultralight ultra-light lightweight
    ergonomic ambidextrous programmable macro buttons button dpi sensor polling rate minimalist illuminated
    for with and or the a of in to compatible pc laptop mac windows android ios ps4 ps5 playstation
    switch nintendo console consoles tablet phone mobile
    black white grey gray silver red blue brown yellow green pink purple orange gold cyan lilac mint
    midnight cosmic starlight volcanic galactic camo carbon frost ice snow thunder sea cedar
    edition version series layout full-size full size tkl tenkeyless compact percent
    linear tactile clicky switches
    new latest original genuine official warranty year years
    """.split()
)

# Words allowed to follow a part code ("G502 X Plus", "K70 RGB Pro" keeps "pro").
_MODEL_SUFFIXES = frozenset(
    "x plus pro se max mini lite ultra hero superlight air core elite mk2 mk3 ii iii iv v2 v3 v4 2 3 4 s g2 g3".split()
)

# Tokens that look like part codes but are specs.
_SPEC_TOKEN_RE = re.compile(
    r"^(?:\d+(?:dpi|hz|khz|mah|mm|g|gb|w|ms|ghz|k|%|db|hrs?|h|m)|\d+-?keys?|\d{4,}|"
    r"usb\d|\d\.\d|7\.1|2\.4g(?:hz)?|5\.\d|ps[45]|wifi\d?|20\d{2})$"
)
# Version markers (V3, MK2, MK.2) belong to the model, but are never a model on their own.
_VERSION_RE = re.compile(r"^(?:v\d+|mk\.?\d+|mkii|gen\d+)$")
_CODE_TOKEN_RE = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9][a-z0-9\-]+$")
# Words that sit between a model and its version ("K70 RGB PRO", "G502 X Lightspeed Plus").
_SKIPPABLE = frozenset({"rgb", "argb", "lightsync", "chroma", "led", "lightspeed", "hyperspeed"})
# Manufacturer part numbers that look like model codes: HP/HyperX "6N0A8AA", "4P5M0AA".
_PART_CODE_RE = re.compile(r"^\d[a-z]\d[a-z]\d[a-z]{2}$|^\d[a-z0-9]{6,}$")

# Flagship products that are usually written without a part code.
_KNOWN_MODELS: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "controller": (
        (re.compile(r"\bdualsense\s+edge\b"), "dualsense-edge"),
        (re.compile(r"\bdualsense\b"), "dualsense"),
        (re.compile(r"\bdualshock\s*4\b"), "dualshock-4"),
        (re.compile(r"\bxbox\s+elite\s+(?:wireless\s+)?(?:controller\s+)?series\s*2\b"), "xbox-elite-series-2"),
        (re.compile(r"\bxbox\s+(?:wireless\s+)?(?:core\s+)?controller\b|\bxbox\s+wireless\b"), "xbox-wireless"),
    ),
}

# First-party controllers are always wireless, whether or not the title says so.
_ALWAYS_WIRELESS = frozenset({"dualsense-edge", "dualsense", "dualshock-4", "xbox-elite-series-2", "xbox-wireless"})

_WIRELESS_RE = re.compile(
    r"\b(?:wireless|bluetooth|tri[\s\-]?mode|dual[\s\-]?mode|2\.4\s*g(?:hz)?|lightspeed|hyperspeed|slipstream|quickswitch)\b"
)
_WIRED_ONLY_RE = re.compile(r"\bwired\b")
_WIRED_AND_WIRELESS_RE = re.compile(r"wired\s*(?:/|and|&|\+|or)\s*wireless|wireless\s*(?:/|and|&|\+|or)\s*wired")

# "<name> switch(es)": up to three words before the word "switch".
_SWITCH_PHRASE_RE = re.compile(r"\b((?:[a-z][a-z\-]*\s+){0,2}[a-z][a-z\-]*)\s+switch(?:es)?\b")
# Switch named by maker + colour without the word "switch" ("Cherry MX Red", "Gateron Brown", "HX Red").
_SWITCH_MAKER_RE = re.compile(
    r"\b(?:gateron|kailh|outemu|cherry\s*mx|mx|huano|akko|jwk|hx|gl|razer)\s+"
    r"(red|blue|brown|yellow|silver|black|white|green|purple|orange|pink|clear|banana|aqua|tactile|linear|clicky)\b"
)
# Words in a switch phrase that are not the switch's name.
_SWITCH_NOISE = frozenset(
    """
    with and the a of for in on pro gaming keyboard keyboards rgb argb led backlit backlight
    mechanical optical linear tactile clicky hot swappable hot-swappable hotswap pre-lubed lubed
    gateron kailh outemu cherry mx huano akko jwk hx gl razer logitech redragon aula custom
    tri mode tri-mode wireless wired nintendo translucent transparent
    """.split()
)

# Named colourways other than black (white is tracked on its own, like every other category).
_COLOURWAYS = (
    "sea blue", "ice blue", "sky blue", "baby blue", "navy blue", "light blue", "dark blue",
    "cedar green", "mint green", "olive green", "light green", "rose pink", "baby pink",
    "blue", "red", "pink", "purple", "lilac", "lavender", "green", "mint", "yellow", "orange",
    "grey", "gray", "silver", "gold", "cyan", "beige", "cream", "teal", "magenta",
    "quartz", "mercury",
)
_COLOUR_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in _COLOURWAYS) + r")\b")
_TKL_RE = re.compile(r"\b(?:tkl|tenkeyless)\b")

# Words that make a different product (different price, different MPN) within a model line.
_PERIPHERAL_VARIANTS: dict[str, tuple[tuple[str, str], ...]] = {
    # "Chroma" is a separate SKU for Razer controllers/headsets (Wolverine V2 vs V2 Chroma), but on
    # keyboards and mice stores add it loosely to the same product, so it isn't used there.
    "controller": ((r"\bchroma\b", "chroma"),),
    "headset": (
        (r"\bchroma\b", "chroma"),
        (r"\bgen(?:eration)?\s*(?:ii|2)\b", "gen2"),
        (r"\bgen(?:eration)?\s*(?:iii|3)\b", "gen3"),
        (r"\btrue\s+wireless\s+3\b", "tw3"),
        (r"\btrue\s+wireless\s+4\b", "tw4"),
        (r"\btrue\s+wireless\s+5\b", "tw5"),
    ),
    "keyboard": ((r"\bhe\b", "he"),),
    "mouse": ((r"\bgen(?:eration)?\s*(?:ii|2)\b", "gen2"),),
}
# Several connection modes listed ("Tri Mode Wireless, Bluetooth, Wired"): it is wireless.
_MULTI_MODE_RE = re.compile(r"\b(?:tri[\s\-]?mode|dual[\s\-]?mode|triple[\s\-]?mode|bluetooth|2\.4\s*g(?:hz)?)\b")


def _brand_words(brand_slug: str | None) -> set[str]:
    entry = BRAND_BY_SLUG.get(brand_slug or "")
    words: set[str] = set()
    if entry:
        for alias in entry.aliases:
            words.update(alias.lower().split())
        words.update(entry.name.lower().split())
    # Aliases with model numbers must not turn those numbers into brand words.
    return {w for w in words if not any(c.isdigit() for c in w)}


def _is_part_number(token: str) -> bool:
    """Store SKUs / manufacturer part numbers such as `rz03-03390100-r3m1` or `920-012726`."""
    return sum(c.isdigit() for c in token) >= 5 or token.count("-") >= 2 or bool(_PART_CODE_RE.match(token))


def _model_tokens(cleaned: str, brand_slug: str | None) -> list[str]:
    brand_words = _brand_words(brand_slug)
    tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9\-\.\+%]*", cleaned) if t not in brand_words]
    out = []
    for t in tokens:
        t = t.rstrip(".-")
        if re.fullmatch(r"mk\.\d+", t):
            t = t.replace(".", "")
        if t and not _is_part_number(t):
            out.append(t)
    return out


def _extract_model(cleaned: str, category: CategorySlug, brand_slug: str | None) -> str | None:
    for pat, slug in _KNOWN_MODELS.get(category, ()):
        if pat.search(cleaned):
            return slug

    tokens = _model_tokens(cleaned, brand_slug)

    # 1) part code + version suffixes ("g502 x plus", "k70 [rgb] pro", "k8 pro")
    for i, tok in enumerate(tokens):
        if _CODE_TOKEN_RE.match(tok) and not _SPEC_TOKEN_RE.match(tok) and not _VERSION_RE.match(tok):
            parts = [tok]
            for nxt in tokens[i + 1 : i + 5]:
                if nxt in _MODEL_SUFFIXES or _VERSION_RE.match(nxt):
                    parts.append(nxt)
                elif nxt in _SKIPPABLE:
                    continue
                else:
                    break
                if len(parts) == 3:
                    break
            return "-".join(parts)

    # 2) leading product-line words ("deathadder v3 pro", "pro x superlight 2 dex")
    words: list[str] = []
    after_skippable = False
    for tok in tokens:
        if words and tok in _SKIPPABLE:
            after_skippable = True  # "Harpoon RGB Pro": look past RGB for a version word
            continue
        if after_skippable:
            if tok in _MODEL_SUFFIXES or _VERSION_RE.match(tok):
                words.append(tok)
            break
        if tok in _FILLER or _SPEC_TOKEN_RE.match(tok) or tok in ("-", "+", "&"):
            if words:
                break
            continue
        words.append(tok)
        if len(words) == 5:
            break
    return "-".join(words) if words else None


def _extract_switch(cleaned: str, exclude: set[str]) -> tuple[str | None, list[tuple[int, int]]]:
    """Keyboard switch name and the text spans it came from (so they aren't read as colours).

    `exclude` holds the brand and model words, which often sit right before "switch"
    ("Huntsman Mini Purple Switch").
    """
    spans: list[tuple[int, int]] = []
    name: str | None = None
    for m in _SWITCH_PHRASE_RE.finditer(cleaned):
        words = [w for w in m.group(1).split() if w not in _SWITCH_NOISE and w not in exclude]
        if words:
            name = name or "-".join(words[-2:])
            spans.append(m.span())
    for m in _SWITCH_MAKER_RE.finditer(cleaned):
        colour = m.group(1)
        if colour not in ("tactile", "linear", "clicky"):
            name = name or colour
        spans.append(m.span())
    return name, spans


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in spans:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


_KNOWN_DISPLAY = {
    "dualsense-edge": "DualSense Edge",
    "dualsense": "DualSense",
    "dualshock-4": "DualShock 4",
    "xbox-elite-series-2": "Xbox Elite Series 2",
    "xbox-wireless": "Xbox Wireless",
}


def model_display_name(model: str, raw_title: str) -> str:
    """The model as the store wrote it ("DeathAdder V3 Pro"), falling back to upper-cased codes."""
    if model in _KNOWN_DISPLAY:
        return _KNOWN_DISPLAY[model]
    shown = []
    for part in model.split("-"):
        m = re.search(rf"(?<![A-Za-z0-9]){re.escape(part)}(?![A-Za-z0-9])", raw_title, re.IGNORECASE)
        original = m.group(0) if m else part
        if original.lower() in ("ii", "iii", "iv", "vi", "vii", "tkl"):
            original = original.upper()
        elif original.islower() or original.isupper():
            # No useful casing from the store: codes upper-case, words capitalised.
            original = (
                original.upper()
                if any(c.isdigit() for c in original) or len(original) <= 2
                else original.capitalize()
            )
        shown.append(original)
    return " ".join(shown)


def extract_peripheral_attributes(
    raw_title: str,
    *,
    category: CategorySlug,
    raw_brand: str | None = None,
    raw_mpn: str | None = None,
    retailer: RetailerSlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    """Extract `brand`, `model`, `connection`, `white`, `colourway` (+ `switch`, `layout` for keyboards)."""
    cleaned = normalize_title(raw_title, retailer=retailer)
    brand_slug, brand_name, brand_conflict = detect_brand(
        raw_title, raw_brand=raw_brand, category=category, source_fields=source_fields
    )
    # "HyperX" is a Kingston alias for memory, but HyperX peripherals are HP's brand.
    if brand_slug == "kingston" and re.search(r"\bhyper\s*x\b", cleaned):
        brand_slug, brand_name = "hyperx", "HyperX"
        brand_conflict = None
    mpn = extract_canonical_mpn(raw_mpn, raw_title, category, source_fields=source_fields)

    conflicts: list[str] = []
    if brand_conflict:
        conflicts.append(brand_conflict)
    colour_conflict = check_colour_conflict(raw_title, source_fields)
    if colour_conflict:
        conflicts.append(colour_conflict)

    model = _extract_model(cleaned, category, brand_slug)
    switch, switch_spans = (
        _extract_switch(cleaned, _brand_words(brand_slug) | set((model or "").split("-")))
        if category == "keyboard"
        else (None, [])
    )
    if category == "keyboard" and model and _TKL_RE.search(cleaned) and "tkl" not in model:
        model = f"{model}-tkl"
    # "BlackShark V2 Pro 2023 Edition" is a newer product than the original V2 Pro.
    m_year = re.search(r"\b(20[12]\d)\s+edition\b", cleaned)
    if model and m_year and m_year.group(1) not in model:
        model = f"{model}-{m_year.group(1)}"
    if model:
        model = append_variants(cleaned, model, _PERIPHERAL_VARIANTS.get(category, ()))
    model_display = model_display_name(model, raw_title) if model else None

    wireless = bool(_WIRELESS_RE.search(cleaned)) or model in _ALWAYS_WIRELESS
    if _WIRED_ONLY_RE.search(cleaned) and not _WIRED_AND_WIRELESS_RE.search(cleaned) and not _MULTI_MODE_RE.search(cleaned):
        wireless = model in _ALWAYS_WIRELESS

    # Colour words that are part of a switch name ("Red Switch") or the model are not colourways.
    colour_text = _blank_spans(cleaned, switch_spans)
    for part in (model or "").split("-"):
        colour_text = re.sub(rf"\b{re.escape(part)}\b", " ", colour_text)
    white = bool(re.search(r"\b(?:white|wht)\b", colour_text))
    m_colour = _COLOUR_RE.search(colour_text)
    colourway = m_colour.group(1).replace(" ", "-").replace("gray", "grey") if m_colour else None

    attrs: dict[str, Any] = {
        "brand": brand_slug,
        "model": model,
        "model_display": model_display,
        "connection": "wireless" if wireless else "wired",
        "white": white,
    }
    if colourway:
        attrs["colourway"] = colourway
    if category == "keyboard":
        if switch:
            attrs["switch"] = switch
        if model and model.endswith("-tkl"):
            attrs["layout"] = "tkl"

    missing = [k for k in ("brand", "model") if not attrs.get(k)]
    return ExtractionResult(
        category=category,
        cleaned_title=cleaned,
        brand_slug=brand_slug,
        brand_name=brand_name,
        mpn=mpn,
        attributes=attrs,
        missing_fields=missing,
        conflict_notes=conflicts,
    )


def _bind(category: CategorySlug):
    def _extract(raw_title: str, **kwargs: Any) -> ExtractionResult:
        return extract_peripheral_attributes(raw_title, category=category, **kwargs)

    _extract.__name__ = f"extract_{category}_attributes"
    return _extract


extract_keyboard_attributes = _bind("keyboard")
extract_mouse_attributes = _bind("mouse")
extract_headset_attributes = _bind("headset")
extract_controller_attributes = _bind("controller")
