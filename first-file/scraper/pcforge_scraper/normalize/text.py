"""Price parsing, URL cleaning, title normalization, unit unification, and MPN extraction."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pcforge_scraper.config import CategorySlug, RetailerSlug

# Matches a currency/number token like "₹1,04,994.00", "Rs. 9,450", "29874.00"
_PRICE_TOKEN_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

# Tracking and pagination query parameters stripped from canonical listing URLs
_STRIP_QUERY_PARAMS = frozenset(
    {
        "limit",
        "sort",
        "order",
        "page",
        "filter_name",
        "filter_description",
        "filter_sub_category",
        "tag",
        "linkcode",
        "camp",
        "creative",
        "creativeasin",
        "ascsubtag",
        "ref",
        "ref_",
        "psc",
        "qid",
        "sr",
        "affid",
        "affextparam1",
        "affextparam2",
        "cmpid",
        "pid",
        "lid",
        "marketplace",
        "store",
        "srno",
        "otracker",
        "otracker1",
        "fm",
        "iid",
        "ppt",
        "ppn",
        "ssid",
        "qhu",
        "variant",
        "_pos",
        "_sid",
        "_ss",
        "fbclid",
        "gclid",
        "tracking",
    }
)

# Marketing parenthetical clauses to strip (never strips parentheticals containing kit config, CL, white/black, tray/box, etc.)
_MARKETING_PAREN_RE = re.compile(
    r"\(\s*(?:"
    r"\d+(?:\.\d+)?\s*ghz[^)]*|"
    r"\d+\s*cores?[^)]*|"
    r"up\s+to\s+\d+(?:\.\d+)?\s*ghz[^)]*|"
    r"max\.?\s*boost[^)]*|"
    r"indian\s+retail[^)]*|"
    r"intel\s+xmp[^)]*|"
    r"amd\s+expo[^)]*|"
    r"low\s+noise[^)]*|"
    r"socket\s+[^)]*|"
    r"pcie\s+[^)]*|"
    r"without\s+cooler|"
    r"no\s+cooler"
    r")\s*\)",
    re.IGNORECASE,
)

# Long Amazon/Flipkart trailing marketing clauses after " | " or " - " (preserving "- White" / "- Black" / "- Grey")
_MARKETPLACE_TAIL_RE = re.compile(
    r"(?:\s+\|\s+|\s+-\s+(?!(?:black|white|grey|gray|silver|red|blue|argb|rgb|oc|wifi|v\d)\b))"
    r"(?:unlocked\s+desktop\s+processor|desktop\s+processor\s+for\s+gaming|"
    r"pci\s+express\s+graphics\s+card|gaming\s+graphics\s+card\s*-|"
    r"desktop\s+motherboard|gaming\s+motherboard\s+with\s+pcie|"
    r"high\s+performance\s+desktop\s+memory|desktop\s+ram\s+memory\s+module|"
    r"certified\s+switching\s+power\s+supply|atx\s+power\s+supply\s+unit\s+for|"
    r"gaming\s+pc\s+cabinet\s+computer\s+case|mid-tower\s*/\s*atx\s+computer\s+gaming\s+cabinet).*$",
    re.IGNORECASE,
)

# Generic noise phrases stripped during normalization (variant words are NEVER in this list)
_NOISE_PHRASES_RE = re.compile(
    r"\b(?:"
    r"graphics\s+cards?|graphic\s+cards?|video\s+cards?|"
    r"desktop\s+processors?|gaming\s+processors?|processors?|"
    r"with\s+radeon(?:\s+rx)?(?:\s+vega\s+\d+)?\s+graphics|"
    r"with\s+intel\s+uhd\s+graphics(?:\s+\d+)?|"
    r"with\s+carbice\s+ice\s+pad|"
    r"10th\s+anniversary\s+edition|"
    r"\d+(?:st|nd|rd|th)\s+gen(?:eration)?|"
    r"desktop\s+motherboards?|gaming\s+motherboards?|motherboards?|mainboards?|"
    r"desktop\s+memory|memory\s+modules?|memory\s+kit|"
    r"power\s+supply\s+units?|power\s+supply|switching\s+power\s+supply|smps|psu|"
    r"computer\s+cases?|pc\s+cabinets?|gaming\s+cabinets?|cabinets?|"
    r"pci\s+express\s+\d+(?:\.\d+)?|"
    r"gddr[34567]x?|"
    r"1\.\d+v"
    r")\b",
    re.IGNORECASE,
)

# Patterns for genuine manufacturer part numbers embedded in titles or MPN fields
_TITLE_MPN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AMD retail/tray CPU part numbers: 100-100000593WOF, 100-100000065BOX, 100-000000593MPK
    re.compile(r"\b(100-[01]\d{8}(?:WOF|BOX|POF|MPK|TRAY)?)\b", re.IGNORECASE),
    # Intel retail/tray CPU part numbers: BX8071512100F, CM8071504555317
    re.compile(r"\b((?:BX|CM|BXC)80\d{3}[A-Z0-9]{4,10})\b", re.IGNORECASE),
    # Gigabyte GPU/MB part numbers: GV-N4060GAMING-OC-8GD, GV-R76GAMING-OC-8GD
    re.compile(r"\b(GV-[NR][A-Z0-9\-]{6,25})\b", re.IGNORECASE),
    # ASUS 90YV / 90MB / DUAL- / TUF- / ROG-STRIX- part numbers
    re.compile(r"\b(90(?:YV|MB|NB|MP)[A-Z0-9\-]{6,20})\b", re.IGNORECASE),
    re.compile(r"\b((?:DUAL|TUF|ROG-STRIX)-(?:RTX|RX|GTX)[A-Z0-9\-]{5,25})\b", re.IGNORECASE),
    # Zotac GPU part numbers: ZT-A30510H-10L, ZT-D40600E-10M
    re.compile(r"\b(ZT-[A-Z0-9]{5,10}-[A-Z0-9]{2,6})\b", re.IGNORECASE),
    # Inno3D GPU part numbers: N30502-08D6-1711VA41, N50502-08D6-174071N
    re.compile(r"\b(N\d{5}-\d{2}D\d-[A-Z0-9]{5,12})\b", re.IGNORECASE),
    # Corsair RAM / PSU part numbers: CMK16GX5M1B5200C40, CMH32GX5M2B6000Z30K, CP-9020296-IN
    re.compile(r"\b(CM[KHW]\d+GX[45]M\d[A-Z0-9]{6,14})\b", re.IGNORECASE),
    re.compile(r"\b(CP-9020\d{3}-[A-Z]{2,4})\b", re.IGNORECASE),
    # G.Skill RAM part numbers: F5-6000J3038F16GX2-TZ5NR, F4-3200C16S-16GVK
    re.compile(r"\b(F[45]-\d{4}[A-Z]\d{2,4}[A-Z0-9\-]{4,16})\b", re.IGNORECASE),
    # Kingston Fury RAM part numbers: KF560C36BBEK2-32, KF432C16BB/16
    re.compile(r"\b(KF[45]\d{2}C\d{2}[A-Z0-9/\-]{3,14})\b", re.IGNORECASE),
    # ADATA / XPG RAM part numbers: AX5U6000C3616G-DTLABWH, AD5U560016G-S
    re.compile(r"\b(A[XD][45]U\d{4}[A-Z0-9\-]{4,18})\b", re.IGNORECASE),
    # TeamGroup RAM part numbers: FF3D532G6000HC38A01, FLRD532G6000HC38A01
    re.compile(r"\b(F[A-Z]{2,3}D[45]\d{2}G\d{4}HC\d{2}[A-Z0-9]{2,6})\b", re.IGNORECASE),
    # ASRock GPU part numbers: RX7600-CL-8GO, RX9070XT-CL-16G
    re.compile(r"\b(RX\d{4}(?:XT|XTX)?-[A-Z]{2,4}-\d{1,2}G[A-Z0-9]*)\b", re.IGNORECASE),
    # PNY GPU part numbers: VCG40608DFXPB1, VCNRTXA400-SB
    re.compile(r"\b(VC[GN][A-Z0-9\-]{6,18})\b", re.IGNORECASE),
)

# Rejects slugified titles or generic descriptors that some stores place in `sku` / `raw_mpn`
_FAKE_MPN_PREFIXES = (
    "amd-ryzen-",
    "ryzen-",
    "intel-core-",
    "core-i",
    "i3-",
    "i5-",
    "i7-",
    "i9-",
    "deepcool-",
    "corsair-",
    "msi-mag-",
    "msi-mpg-",
    "msi-pro-",
    "asrock-",
    "ant-value-",
    "radeon-rx-",
    "geforce-rtx-",
    "amd-r",
    "asus-",
    "gigabyte-",
    "zotac-",
    "cooler-master-",
)


def parse_price_paise(raw: str | float | Decimal | None) -> int | None:
    """Parse an INR price string or number into integer paise.

    Examples:
        >>> parse_price_paise("₹1,04,994")
        10499400
        >>> parse_price_paise("₹17,290.00")
        1729000
        >>> parse_price_paise("Rs. 9,450.50")
        945050
        >>> parse_price_paise("0") is None
        True
    """
    if raw is None or isinstance(raw, bool):
        return None

    if isinstance(raw, (int, float, Decimal)):
        try:
            dec = Decimal(str(raw))
        except InvalidOperation:
            return None
        paise = int((dec * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return paise if paise > 0 else None

    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text:
        return None

    for match in _PRICE_TOKEN_RE.finditer(text):
        num_str = match.group(1).replace(",", "")
        if not num_str:
            continue
        try:
            dec = Decimal(num_str)
        except InvalidOperation:
            continue
        paise = int((dec * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if paise > 0:
            return paise

    return None


def clean_product_url(url: str) -> str:
    """Return a clean, canonical product URL with affiliate/tracking/pagination params stripped."""
    raw = url.strip()
    if not raw:
        raise ValueError("Product URL cannot be empty")

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Invalid product URL (missing scheme or host): {url!r}")

    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path)

    # Amazon canonicalization: reduce /.../dp/<ASIN>/... to /dp/<ASIN>
    if "amazon.in" in netloc:
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", path, re.IGNORECASE)
        if m:
            path = f"/dp/{m.group(1).upper()}"
            return urlunsplit((scheme, "www.amazon.in", path, "", ""))

    # Flipkart deep-link canonicalization: dl.flipkart.com/dl/... -> www.flipkart.com/...
    if netloc == "dl.flipkart.com":
        netloc = "www.flipkart.com"
        if path.startswith("/dl/"):
            path = path[3:]

    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    kept_params: list[tuple[str, str]] = []
    for k, v in parse_qsl(parts.query, keep_blank_values=False):
        k_lower = k.lower()
        if k_lower.startswith("utm_") or k_lower in _STRIP_QUERY_PARAMS:
            continue
        kept_params.append((k, v))

    query = urlencode(kept_params, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


_TRADEMARK_RE = re.compile("[™®©�]")


def _strip_trademarks(text: str) -> str:
    """Drop TM/(R)/(C) marks first: NFKC would turn "RTX™ 5060" into "RTXTM 5060"."""
    return _TRADEMARK_RE.sub("", text)


def clean_title_basic(title: str) -> str:
    """Normalize unicode, whitespace, and punctuation for basic title matching."""
    text = unicodedata.normalize("NFKC", _strip_trademarks(title)).lower()
    text = re.sub(r"[\u2010-\u2015_/\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_store_title(raw_title: str, retailer: RetailerSlug | None = None) -> str:
    """Apply store-specific cleanup hooks (including long Amazon/Flipkart title noise stripping)."""
    text = unicodedata.normalize("NFKC", _strip_trademarks(raw_title)).strip()
    # Strip long marketplace SEO suffixes (" | Unlocked Desktop Processor...", " - PCI Express Graphics Card...")
    text = _MARKETPLACE_TAIL_RE.sub("", text)
    # Strip bracketed MPNs like "[100-100000593WOF]" or "[CP-9020296-IN]" (MPN is extracted from raw_title separately)
    text = re.sub(r"\[\s*[A-Z0-9][A-Z0-9\-/._\s]{3,35}\s*\]", " ", text)
    # Strip parenthetical part numbers like "(100-100000593WOF)", "(C700-KGNN-S00)", "(RX7600-CL-8GO)"
    # while never stripping kit configs like "(16GBx2)" or "(2x16GB)" or "(CL30)"
    text = re.sub(
        r"\(\s*(?![0-9]{1,3}\s*gb|\d\s*x\s*\d|cl\s*\d{2})[A-Z0-9]{2,10}-[A-Z0-9\-/._]{2,25}\s*\)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    # Strip marketing parentheticals like "(6 Cores, 12 Threads...)" or "(4.6 GHz / 8 Cores / 16 Threads)"
    text = _MARKETING_PAREN_RE.sub(" ", text)
    if retailer == "elitehubs":
        # EliteHubs appends "( 750W )" at the end of PSU titles when wattage is already in the title
        text = re.sub(r"\(\s*\d{3,4}\s*w\s*\)\s*$", "", text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip()


def _normalize_kit_token(match: re.Match[str]) -> str:
    """Normalize RAM kit expressions (`16GBx2`, `16GB x 2`, `2x16GB`, `2 x 16 GB`) into `<sticks>x<size>gb`."""
    first_num = int(match.group("n1"))
    first_unit = (match.group("u1") or "").lower()
    second_num = int(match.group("n2"))
    second_unit = (match.group("u2") or "").lower()

    if first_unit == "gb" and not second_unit and second_num in (1, 2, 4, 8):
        # e.g. "16GBx2" or "16GB x 1"
        return f"{second_num}x{first_num}gb"
    if second_unit == "gb" and not first_unit and first_num in (1, 2, 4, 8):
        # e.g. "2x16GB" or "1 x 16 GB"
        return f"{first_num}x{second_num}gb"
    if first_num in (1, 2, 4, 8) and second_num >= 4:
        return f"{first_num}x{second_num}gb"
    if second_num in (1, 2, 4, 8) and first_num >= 4:
        return f"{second_num}x{first_num}gb"
    return match.group(0).lower()


_KIT_CONFIG_RE = re.compile(
    r"\b(?P<n1>\d{1,3})\s*(?P<u1>gb)?\s*[x×*]\s*(?P<n2>\d{1,3})\s*(?P<u2>gb)?\b",
    re.IGNORECASE,
)


def normalize_title(raw_title: str, retailer: RetailerSlug | None = None) -> str:
    """Run Steps 1, 2, and 5 of the §5.2 Normalization Pipeline on a product title.

    1. Clean: Unicode NFKC, lowercase, strip marketing clauses, unify punctuation.
    2. Unit-normalize:
       - `2 x 16 GB` / `16GBx2` / `16GB x 2` -> `2x16gb`
       - `8 GB` / `8GB` / `8Gb` -> `8gb`
       - `6000MHz` / `6000 MHz` / `6000 MT/s` -> `6000`
       - `850 W` / `850 Watt` / `850 Watts` -> `850w`
       - `CL 30` / `CL-30` / `C30` (in RAM context) -> `cl30`
    3. Synonyms:
       - `oc edition` / `overclocked` -> `oc`
       - `wi-fi 6e` / `wifi 6e` / `wi-fi 6` / `wifi 6` / `wi-fi` -> `wifi`
       - `fully modular` / `full modular` -> `full-modular`
       - `semi modular` -> `semi-modular`
       - `non modular` -> `non-modular`
       - `micro atx` / `m-atx` -> `matx`
    4. Strip generic noise words while NEVER stripping variant tokens (`oc`, `white`, `wifi`, `ti`, `x3d`, `tray`).
    """
    text = clean_store_title(raw_title, retailer=retailer).lower()

    # Preserve "80+" / "80 plus" before punctuation normalization
    text = re.sub(r"\b80\s*\+\s*", "80plus ", text)
    text = re.sub(r"\b80\s+plus\b", "80plus", text)

    # Preserve "nitro+" as "nitro-plus"
    text = re.sub(r"\bnitro\s*\+", "nitro-plus", text)

    # Unify RAM kit configurations BEFORE general unit normalization (`16GB x 2` / `2x16GB` -> `2x16gb`)
    # Avoid matching fan dimensions like `2x 160mm` by requiring `gb` on at least one side
    def _kit_replacer(m: re.Match[str]) -> str:
        u1 = m.group("u1")
        u2 = m.group("u2")
        if not u1 and not u2:
            return m.group(0)
        return _normalize_kit_token(m)

    text = _KIT_CONFIG_RE.sub(_kit_replacer, text)

    # Unit-normalize capacities: "8 GB", "16 GB", "32GB" -> "8gb", "16gb", "32gb"
    text = re.sub(r"\b(\d{1,4})\s*gb\b", r"\1gb", text)
    # Also normalize ASUS/MSI GPU VRAM shorthand like "O8G" / "8G" when followed by GDDR or end of word
    text = re.sub(r"\bo(\d{1,2})g\b", r"oc \1gb", text)

    # Unit-normalize memory speeds: "6000MHz", "6000 MHz", "6000 MT/s" -> "6000"
    text = re.sub(r"\b(\d{4})\s*(?:mhz|mt/s|mts)\b", r"\1", text)

    # Unit-normalize PSU wattages: "850 W", "850 Watt", "850 Watts", "850W" -> "850w"
    text = re.sub(r"\b(\d{3,4})\s*(?:watts?|w)\b", r"\1w", text)

    # Normalize CAS latency: "CL 30", "CL-30", "CL30", or standalone "C40"/"C36"/"C16" -> "cl30"
    text = re.sub(r"\bcl[\s\-]?(\d{2})\b", r"cl\1", text)
    text = re.sub(r"\bc(1[45689]|2[02468]|3[02468]|4[026])\b", r"cl\1", text)

    # Synonyms (§5.2 step 5)
    text = re.sub(r"\b(?:oc\s+edition|overclocked\s+edition|overclocked)\b", "oc", text)
    text = re.sub(r"\b(?:wi[\s\-]?fi(?:\s*6e|\s*6|\s*7)?|wifi6e|wifi6|wifi7)\b", "wifi", text)
    text = re.sub(r"\b(?:fully[\s\-]+modular|full[\s\-]+modular)\b", "full-modular", text)
    text = re.sub(r"\bsemi[\s\-]+modular\b", "semi-modular", text)
    text = re.sub(r"\bnon[\s\-]+modular\b", "non-modular", text)
    text = re.sub(r"\b(?:micro[\s\-]*atx|m[\s\-]*atx)\b", "matx", text)
    text = re.sub(r"\b(?:mini[\s\-]*itx|m[\s\-]*itx)\b", "itx", text)
    text = re.sub(r"\b(?:extended[\s\-]*atx|e[\s\-]*atx)\b", "eatx", text)

    # Strip generic noise phrases (never strips variant words)
    text = _NOISE_PHRASES_RE.sub(" ", text)

    # Replace brackets/parens/punctuation with spaces (preserving hyphens inside tokens like full-modular, 2x16gb)
    text = re.sub(r"[()\[\]{},;:|/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_valid_manufacturer_mpn(candidate: str, category: CategorySlug | None = None) -> bool:
    """Return True if `candidate` looks like a genuine SKU/MPN rather than a slugified title or generic word."""
    cleaned = candidate.strip().upper()
    if len(cleaned) < 5 or len(cleaned) > 40:
        return False
    # Must not contain spaces (except genuine Gigabyte/ASRock board codes which we disallow if generic)
    if " " in cleaned:
        return False
    # Must contain at least one digit and one letter
    if not (any(c.isdigit() for c in cleaned) and any(c.isalpha() for c in cleaned)):
        return False

    lower = cleaned.lower()
    if lower.startswith(_FAKE_MPN_PREFIXES):
        return False
    # Reject synthetic fallback codes like "ASUS-GPU-01" or "AMD-CPU-02"
    if re.match(r"^[a-z0-9\-]+-(?:cpu|gpu|motherboard|ram|psu|case)-\d+$", lower):
        return False
    # Reject bare model tokens like "450-STEEL-LEGEND", "4070-AX", "RX550", "VS450L", "B550M-K"
    if category == "cpu":
        return bool(
            re.match(r"^(?:100-[01]\d{8}[A-Z]*|(?:BX|CM|BXC)80\d{3}[A-Z0-9]+)$", cleaned)
        )
    return any(pat.fullmatch(cleaned) for pat in _TITLE_MPN_PATTERNS)


def extract_canonical_mpn(
    raw_mpn: str | None,
    raw_title: str,
    category: CategorySlug,
    source_fields: Mapping[str, Any] | None = None,
) -> str | None:
    """Extract and validate a canonical uppercase MPN from API fields, `raw_mpn`, or `raw_title`."""
    candidates: list[str] = []

    if source_fields:
        for key in ("part_number", "model", "mpn", "sku"):
            val = source_fields.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        attrs = source_fields.get("attributes")
        if isinstance(attrs, Mapping):
            for key in ("partNumber", "modelNumber", "mpn"):
                val = attrs.get(key)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())

    if raw_mpn and raw_mpn.strip():
        candidates.append(raw_mpn.strip())

    for cand in candidates:
        normalized_cand = re.sub(r"\s+", "", cand).upper()
        if is_valid_manufacturer_mpn(normalized_cand, category=category):
            return normalized_cand

    # Fallback: scan `raw_title` for embedded manufacturer part numbers
    for pat in _TITLE_MPN_PATTERNS:
        m = pat.search(raw_title)
        if m:
            found = m.group(1).strip().upper()
            if is_valid_manufacturer_mpn(found, category=category):
                return found

    return None
