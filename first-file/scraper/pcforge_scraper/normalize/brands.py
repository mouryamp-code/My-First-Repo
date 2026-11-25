"""Brand alias dictionary and category-aware brand resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pcforge_scraper.config import CategorySlug


@dataclass(frozen=True)
class BrandEntry:
    """Canonical brand definition matching the `brand` table in PostgreSQL."""

    slug: str
    name: str
    aliases: tuple[str, ...]


# Matches the 30 seeded brands in web/prisma/seed.ts plus common Indian PC component brands
BRAND_CATALOG: tuple[BrandEntry, ...] = (
    BrandEntry(
        slug="amd",
        name="AMD",
        aliases=("amd", "advanced micro devices", "amd ryzen", "amd radeon"),
    ),
    BrandEntry(
        slug="intel",
        name="Intel",
        aliases=("intel", "intel core", "intel arc"),
    ),
    BrandEntry(
        slug="nvidia",
        name="NVIDIA",
        aliases=("nvidia", "geforce"),
    ),
    BrandEntry(
        slug="asus",
        name="ASUS",
        aliases=(
            "asus",
            "asus rog",
            "rog strix",
            "rog",
            "asus tuf",
            "tuf gaming",
            "asus prime",
            "asus proart",
            "proart",
            "asus dual",
        ),
    ),
    BrandEntry(
        slug="msi",
        name="MSI",
        aliases=(
            "msi",
            "micro-star",
            "micro star",
            "msi mag",
            "msi mpg",
            "msi meg",
            "msi pro",
        ),
    ),
    BrandEntry(
        slug="gigabyte",
        name="Gigabyte",
        aliases=(
            "gigabyte",
            "aorus",
            "gigabyte aorus",
            "gigabyte gaming",
            "gigabyte eagle",
            "gigabyte windforce",
            "gigabyte aero",
        ),
    ),
    BrandEntry(
        slug="asrock",
        name="ASRock",
        aliases=(
            "asrock",
            "asrock phantom gaming",
            "asrock steel legend",
            "asrock taichi",
            "asrock challenger",
        ),
    ),
    BrandEntry(
        slug="zotac",
        name="Zotac",
        aliases=("zotac", "zotac gaming"),
    ),
    BrandEntry(
        slug="galax",
        name="GALAX",
        aliases=("galax", "kfa2"),
    ),
    BrandEntry(
        slug="inno3d",
        name="Inno3D",
        aliases=("inno3d", "inno 3d", "ichill"),
    ),
    BrandEntry(
        slug="colorful",
        name="Colorful",
        aliases=("colorful", "igame", "colorful igame"),
    ),
    BrandEntry(
        slug="sapphire",
        name="Sapphire",
        aliases=("sapphire", "sapphire pulse", "sapphire nitro+", "sapphire nitro", "sapphire pure"),
    ),
    BrandEntry(
        slug="powercolor",
        name="PowerColor",
        aliases=(
            "powercolor",
            "power color",
            "powercolor hellhound",
            "powercolor red devil",
            "powercolor fighter",
            "red devil",
            "hellhound",
        ),
    ),
    BrandEntry(
        slug="corsair",
        name="Corsair",
        aliases=("corsair", "corsair vengeance", "corsair dominator"),
    ),
    BrandEntry(
        slug="gskill",
        name="G.Skill",
        aliases=(
            "g.skill",
            "gskill",
            "g skill",
            "g-skill",
            "trident z5",
            "trident z",
            "ripjaws",
            "flare x5",
        ),
    ),
    BrandEntry(
        slug="kingston",
        name="Kingston",
        aliases=("kingston", "kingston fury", "fury beast", "fury renegade", "hyperx"),
    ),
    BrandEntry(
        slug="crucial",
        name="Crucial",
        aliases=("crucial", "crucial pro", "micronaut", "ballistix"),
    ),
    BrandEntry(
        slug="xpg",
        name="XPG",
        aliases=(
            "xpg",
            "adata",
            "adata xpg",
            "xpg lancer",
            "xpg gammix",
            "xpg spectrix",
            "xpg core reactor",
            "xpg battlecruiser",
        ),
    ),
    BrandEntry(
        slug="teamgroup",
        name="TeamGroup",
        aliases=(
            "teamgroup",
            "team group",
            "t-force",
            "t force",
            "team t-force",
            "t-create",
        ),
    ),
    BrandEntry(
        slug="cooler-master",
        name="Cooler Master",
        aliases=("cooler master", "coolermaster"),
    ),
    BrandEntry(
        slug="deepcool",
        name="DeepCool",
        aliases=("deepcool", "deep cool", "gamer storm", "deepcool gamer storm"),
    ),
    BrandEntry(
        slug="lian-li",
        name="Lian Li",
        aliases=("lian li", "lian-li", "lancool"),
    ),
    BrandEntry(
        slug="nzxt",
        name="NZXT",
        aliases=("nzxt",),
    ),
    BrandEntry(
        slug="ant-esports",
        name="Ant Esports",
        aliases=("ant esports", "antesports", "ant-esports", "ant value", "ant-value"),
    ),
    BrandEntry(
        slug="thermaltake",
        name="Thermaltake",
        aliases=("thermaltake",),
    ),
    BrandEntry(
        slug="seasonic",
        name="Seasonic",
        aliases=("seasonic", "sea sonic"),
    ),
    BrandEntry(
        slug="silverstone",
        name="SilverStone",
        aliases=("silverstone", "silver stone"),
    ),
    BrandEntry(
        slug="fractal-design",
        name="Fractal Design",
        aliases=("fractal design", "fractal"),
    ),
    BrandEntry(
        slug="phanteks",
        name="Phanteks",
        aliases=("phanteks",),
    ),
    BrandEntry(
        slug="montech",
        name="Montech",
        aliases=("montech",),
    ),
    # Additional brands present in Indian retailer feeds
    BrandEntry(
        slug="antec",
        name="Antec",
        aliases=("antec",),
    ),
    BrandEntry(
        slug="aerocool",
        name="AeroCool",
        aliases=("aerocool", "aero cool"),
    ),
    BrandEntry(
        slug="pny",
        name="PNY",
        aliases=("pny", "pny xlr8"),
    ),
    BrandEntry(
        slug="nextron",
        name="Nextron",
        aliases=("nextron",),
    ),
    BrandEntry(
        slug="zebronics",
        name="Zebronics",
        aliases=("zebronics",),
    ),
    BrandEntry(
        slug="circle",
        name="Circle",
        aliases=("circle", "circle gaming"),
    ),
    BrandEntry(
        slug="wd",
        name="Western Digital",
        aliases=(
            "western digital",
            "wd",
            "wd black",
            "wd blue",
            "wd green",
            "wd red",
            "wd purple",
            "wd gold",
            "sandisk",
        ),
    ),
    BrandEntry(
        slug="samsung",
        name="Samsung",
        aliases=("samsung", "samsung odyssey", "samsung 990", "samsung 980", "samsung 870"),
    ),
    BrandEntry(
        slug="seagate",
        name="Seagate",
        aliases=("seagate", "barracuda", "ironwolf", "firecuda", "skyhawk", "exos"),
    ),
    BrandEntry(
        slug="toshiba",
        name="Toshiba",
        aliases=("toshiba", "kioxia"),
    ),
    BrandEntry(
        slug="noctua",
        name="Noctua",
        aliases=("noctua",),
    ),
    BrandEntry(
        slug="arctic",
        name="ARCTIC",
        aliases=("arctic", "arctic cooling", "liquid freezer"),
    ),
    BrandEntry(
        slug="lg",
        name="LG",
        aliases=("lg", "lg ultragear", "ultragear"),
    ),
    BrandEntry(
        slug="acer",
        name="Acer",
        aliases=("acer", "acer nitro", "acer predator"),
    ),
    BrandEntry(
        slug="benq",
        name="BenQ",
        aliases=("benq", "benq mobiuz", "mobiuz", "zowie"),
    ),
    BrandEntry(
        slug="aoc",
        name="AOC",
        aliases=("aoc", "aoc agon"),
    ),
    BrandEntry(
        slug="viewsonic",
        name="ViewSonic",
        aliases=("viewsonic", "view sonic"),
    ),
    BrandEntry(
        slug="dell",
        name="Dell",
        aliases=("dell", "alienware"),
    ),
    # Peripherals (keyboards, mice, headsets, controllers)
    BrandEntry(
        slug="logitech",
        name="Logitech",
        aliases=("logitech", "logitech g", "logi",),
    ),
    BrandEntry(
        slug="razer",
        name="Razer",
        aliases=("razer",),
    ),
    BrandEntry(
        slug="hyperx",
        name="HyperX",
        aliases=("hyperx", "hyper x",),
    ),
    BrandEntry(
        slug="steelseries",
        name="SteelSeries",
        aliases=("steelseries", "steel series",),
    ),
    BrandEntry(
        slug="redragon",
        name="Redragon",
        aliases=("redragon",),
    ),
    BrandEntry(
        slug="cosmic-byte",
        name="Cosmic Byte",
        aliases=("cosmic byte", "cosmicbyte",),
    ),
    BrandEntry(
        slug="evofox",
        name="EvoFox",
        aliases=("evofox", "evo fox",),
    ),
    BrandEntry(
        slug="sony",
        name="Sony",
        aliases=("sony", "playstation", "dualsense", "dualshock",),
    ),
    BrandEntry(
        slug="microsoft",
        name="Microsoft",
        aliases=("microsoft", "xbox",),
    ),
    BrandEntry(
        slug="8bitdo",
        name="8BitDo",
        aliases=("8bitdo", "8 bitdo",),
    ),
    BrandEntry(
        slug="gamesir",
        name="GameSir",
        aliases=("gamesir",),
    ),
    BrandEntry(
        slug="keychron",
        name="Keychron",
        aliases=("keychron",),
    ),
    BrandEntry(
        slug="royal-kludge",
        name="Royal Kludge",
        aliases=("royal kludge", "royalkludge",),
    ),
    BrandEntry(
        slug="aula",
        name="AULA",
        aliases=("aula",),
    ),
    BrandEntry(
        slug="glorious",
        name="Glorious",
        aliases=("glorious",),
    ),
    BrandEntry(
        slug="hp",
        name="HP",
        aliases=("hp", "omen",),
    ),
    BrandEntry(
        slug="lenovo",
        name="Lenovo",
        aliases=("lenovo", "legion",),
    ),
    BrandEntry(
        slug="roccat",
        name="ROCCAT",
        aliases=("roccat",),
    ),
    BrandEntry(
        slug="turtle-beach",
        name="Turtle Beach",
        aliases=("turtle beach",),
    ),
    BrandEntry(
        slug="audio-technica",
        name="Audio-Technica",
        aliases=("audio-technica", "audio technica",),
    ),
    BrandEntry(
        slug="sennheiser",
        name="Sennheiser",
        aliases=("sennheiser",),
    ),
    BrandEntry(
        slug="epos",
        name="EPOS",
        aliases=("epos",),
    ),
    BrandEntry(
        slug="jbl",
        name="JBL",
        aliases=("jbl",),
    ),
    BrandEntry(
        slug="boat",
        name="boAt",
        aliases=("boat",),
    ),
    BrandEntry(
        slug="fantech",
        name="Fantech",
        aliases=("fantech",),
    ),
    BrandEntry(
        slug="wooting",
        name="Wooting",
        aliases=("wooting",),
    ),
    BrandEntry(
        slug="havit",
        name="Havit",
        aliases=("havit",),
    ),
    BrandEntry(
        slug="portronics",
        name="Portronics",
        aliases=("portronics",),
    ),
    BrandEntry(
        slug="pulsar",
        name="Pulsar",
        aliases=("pulsar",),
    ),
    BrandEntry(
        slug="lamzu",
        name="Lamzu",
        aliases=("lamzu",),
    ),
    BrandEntry(
        slug="endgame-gear",
        name="Endgame Gear",
        aliases=("endgame gear",),
    ),
    BrandEntry(
        slug="xtrfy",
        name="Xtrfy",
        aliases=("xtrfy",),
    ),
    BrandEntry(
        slug="vgn",
        name="VGN",
        aliases=("vgn",),
    ),
    BrandEntry(
        slug="attack-shark",
        name="Attack Shark",
        aliases=("attack shark",),
    ),
    BrandEntry(
        slug="amkette",
        name="Amkette",
        aliases=("amkette",),
    ),
    BrandEntry(
        slug="marvo",
        name="Marvo",
        aliases=("marvo",),
    ),
    BrandEntry(
        slug="bloody",
        name="Bloody",
        aliases=("bloody", "a4tech",),
    ),
    BrandEntry(
        slug="thrustmaster",
        name="Thrustmaster",
        aliases=("thrustmaster",),
    ),
    BrandEntry(
        slug="nacon",
        name="Nacon",
        aliases=("nacon",),
    ),
    BrandEntry(
        slug="gamdias",
        name="Gamdias",
        aliases=("gamdias",),
    ),
    BrandEntry(
        slug="tvs-electronics",
        name="TVS Electronics",
        aliases=("tvs electronics", "tvs",),
    ),
    BrandEntry(
        slug="mad-catz",
        name="Mad Catz",
        aliases=("mad catz", "madcatz",),
    ),
    BrandEntry(
        slug="karnage",
        name="Karnage",
        aliases=("karnage",),
    ),
    BrandEntry(
        slug="coconut",
        name="Coconut",
        aliases=("coconut",),
    ),
    BrandEntry(
        slug="cybeart",
        name="Cybeart",
        aliases=("cybeart",),
    ),
    BrandEntry(
        slug="cougar",
        name="Cougar",
        aliases=("cougar",),
    ),
    BrandEntry(
        slug="evga",
        name="EVGA",
        aliases=("evga",),
    ),
    BrandEntry(
        slug="super-flower",
        name="Super Flower",
        aliases=("super flower", "superflower",),
    ),
    BrandEntry(
        slug="be-quiet",
        name="be quiet!",
        aliases=("be quiet", "bequiet",),
    ),
    BrandEntry(
        slug="thermalright",
        name="Thermalright",
        aliases=("thermalright",),
    ),
    BrandEntry(
        slug="dawg",
        name="Dawg",
        aliases=("dawg",),
    ),
    BrandEntry(
        slug="prolab",
        name="ProLab Design",
        aliases=("prolab design", "prolab",),
    ),
    BrandEntry(
        slug="hyte",
        name="HYTE",
        aliases=("hyte",),
    ),
    BrandEntry(
        slug="lexar",
        name="Lexar",
        aliases=("lexar",),
    ),
    BrandEntry(
        slug="hikvision",
        name="Hikvision",
        aliases=("hikvision", "hiksemi",),
    ),
    BrandEntry(
        slug="patriot",
        name="Patriot",
        aliases=("patriot",),
    ),
    BrandEntry(
        slug="apacer",
        name="Apacer",
        aliases=("apacer",),
    ),
    BrandEntry(
        slug="klevv",
        name="Klevv",
        aliases=("klevv",),
    ),
    BrandEntry(
        slug="synology",
        name="Synology",
        aliases=("synology",),
    ),
    BrandEntry(
        slug="evm",
        name="EVM",
        aliases=("evm",),
    ),
    BrandEntry(
        slug="pccooler",
        name="PCCooler",
        aliases=("pccooler", "pc cooler",),
    ),
    BrandEntry(
        slug="powera",
        name="PowerA",
        aliases=("powera", "power a",),
    ),
)

BRAND_BY_SLUG: dict[str, BrandEntry] = {b.slug: b for b in BRAND_CATALOG}

# Precompile longest-first word-boundary patterns for deterministic alias lookup
_ALIAS_PATTERNS: list[tuple[re.Pattern[str], BrandEntry]] = []
for _entry in BRAND_CATALOG:
    for _alias in sorted(_entry.aliases, key=len, reverse=True):
        _escaped = re.escape(_alias.lower()).replace(r"\ ", r"[\s\-]+")
        _pat = re.compile(rf"(?<![a-z0-9]){_escaped}(?![a-z0-9])", re.IGNORECASE)
        _ALIAS_PATTERNS.append((_pat, _entry))
_ALIAS_PATTERNS.sort(key=lambda pair: len(pair[0].pattern), reverse=True)

_GPU_CHIPMAKERS = frozenset({"nvidia", "amd", "intel"})


def resolve_brand_alias(raw_brand: str | None) -> BrandEntry | None:
    """Resolve a raw brand string (e.g. `'ASUS ROG'`, `'Adata'`, `'G.Skill'`) to a canonical `BrandEntry`."""
    if not raw_brand or not raw_brand.strip():
        return None
    cleaned = raw_brand.strip().lower()
    if cleaned in BRAND_BY_SLUG:
        return BRAND_BY_SLUG[cleaned]
    for pat, entry in _ALIAS_PATTERNS:
        if pat.search(cleaned):
            return entry
    return None


def detect_brand(
    raw_title: str,
    raw_brand: str | None = None,
    category: CategorySlug | None = None,
    source_fields: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Detect canonical `(brand_slug, brand_name, conflict_note)` from title and structured fields.

    Category rules:
    - `cpu`: Brand is `amd` or `intel`. If `raw_brand` explicitly says `Intel` on an `AMD Ryzen` title
      (or vice versa), returns a conflict note for `REVIEW`.
    - `gpu`: Brand is the board partner (`asus`, `msi`, `gigabyte`, `zotac`, `sapphire`, etc.), not the
      GPU chipmaker (`nvidia` / `amd` / `intel`), unless no board partner is present and the card is a
      genuine reference/workstation card (e.g., `NVIDIA Quadro`, `NVIDIA Founders Edition`, `Intel Arc`).
    - All categories: If `raw_brand` (or `source_fields`) specifies a non-chipmaker brand that contradicts
      the brand in `raw_title`, returns a conflict note so the listing goes to `REVIEW`.
    """
    title_lower = raw_title.lower()

    # Collect all brand matches in the title ordered by earliest character position in title
    matches_in_title: list[tuple[int, int, BrandEntry]] = []
    seen_slugs: set[str] = set()
    for pat, entry in _ALIAS_PATTERNS:
        m = pat.search(title_lower)
        if m and entry.slug not in seen_slugs:
            seen_slugs.add(entry.slug)
            matches_in_title.append((m.start(), -len(m.group(0)), entry))

    matches_in_title.sort(key=lambda item: (item[0], item[1]))
    title_entries = [item[2] for item in matches_in_title]

    # Resolve structured brand from `raw_brand` or `source_fields`
    struct_brand_str = raw_brand
    if not struct_brand_str and source_fields:
        struct_brand_str = str(source_fields.get("brand") or "").strip() or None
    struct_entry = resolve_brand_alias(struct_brand_str)

    if category == "cpu":
        cpu_title_entries = [e for e in title_entries if e.slug in ("amd", "intel")]
        if not cpu_title_entries:
            if re.search(r"\b(?:ryzen|threadripper|athlon)\b", title_lower):
                cpu_title_entries = [BRAND_BY_SLUG["amd"]]
            elif re.search(r"\b(?:core\s+i[3579]|core\s+ultra|pentium|celeron|xeon)\b", title_lower):
                cpu_title_entries = [BRAND_BY_SLUG["intel"]]

        title_brand = cpu_title_entries[0] if cpu_title_entries else None
        if struct_entry and struct_entry.slug in ("amd", "intel") and title_brand:
            if struct_entry.slug != title_brand.slug:
                return (
                    title_brand.slug,
                    title_brand.name,
                    f"Brand conflict: structured brand '{struct_entry.name}' disagrees with title brand '{title_brand.name}'",
                )
        chosen = title_brand or (struct_entry if struct_entry and struct_entry.slug in ("amd", "intel") else None)
        return (chosen.slug, chosen.name, None) if chosen else (None, None, None)

    if category == "gpu":
        partner_entries = [e for e in title_entries if e.slug not in _GPU_CHIPMAKERS]
        chip_entries = [e for e in title_entries if e.slug in _GPU_CHIPMAKERS]

        if partner_entries:
            title_partner = partner_entries[0]
            if struct_entry and struct_entry.slug not in _GPU_CHIPMAKERS:
                if struct_entry.slug != title_partner.slug:
                    return (
                        title_partner.slug,
                        title_partner.name,
                        f"Brand conflict: structured brand '{struct_entry.name}' disagrees with title board partner '{title_partner.name}'",
                    )
            return title_partner.slug, title_partner.name, None

        if struct_entry and struct_entry.slug not in _GPU_CHIPMAKERS:
            return struct_entry.slug, struct_entry.name, None

        # Reference / Founders Edition / Workstation GPU (e.g. NVIDIA Quadro RTX A400)
        if chip_entries and re.search(r"\b(?:quadro|founders\s+edition|rtx\s+a\d{3,4})\b", title_lower):
            return chip_entries[0].slug, chip_entries[0].name, None

        return None, None, None

    # Motherboard, RAM, PSU, Case: ignore `amd`/`intel`/`nvidia` CPU/chipset mentions when board/component brand is present
    non_chip_entries = [e for e in title_entries if e.slug not in _GPU_CHIPMAKERS]
    title_primary = non_chip_entries[0] if non_chip_entries else (title_entries[0] if title_entries else None)

    if title_primary and struct_entry and struct_entry.slug not in _GPU_CHIPMAKERS:
        if struct_entry.slug != title_primary.slug:
            return (
                title_primary.slug,
                title_primary.name,
                f"Brand conflict: structured brand '{struct_entry.name}' disagrees with title brand '{title_primary.name}'",
            )

    chosen = title_primary or struct_entry
    if chosen is None:
        return None, None, None
    return chosen.slug, chosen.name, None

