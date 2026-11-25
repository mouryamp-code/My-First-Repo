"""Fixed-order match key builder and canonical product name/slug generator (`Architecture.md` §5.3)."""

from __future__ import annotations

import re

from pcforge_scraper.normalize.attributes.base import ExtractionResult
from pcforge_scraper.normalize.brands import BRAND_BY_SLUG


def build_match_key(extraction: ExtractionResult) -> str | None:
    """Build the deterministic `|Context`-joined `match_key` if all required fields are valid.

    Returns `None` if any required field is missing or if a structured-field conflict exists.
    """
    if not extraction.is_valid_for_key:
        return None

    cat = extraction.category
    a = extraction.attributes

    if cat == "cpu":
        parts = ["cpu", str(a["brand"]), str(a["model"]), str(a["packaging"])]
        if a.get("with_cooler"):
            parts.append("with-cooler")
        return "|".join(parts).lower()

    if cat == "gpu":
        parts = ["gpu", str(a["brand"]), str(a["chipset"]), str(a["vram"]), str(a["series"])]
        if a.get("oc"):
            parts.append("oc")
        if a.get("white"):
            parts.append("white")
        return "|".join(parts).lower()

    if cat == "motherboard":
        parts = ["mb", str(a["brand"]), str(a["model"]), str(a["memory_type"]), str(a["form_factor"])]
        if a.get("wifi"):
            parts.append("wifi")
        if a.get("white"):
            parts.append("white")
        return "|".join(parts).lower()

    if cat == "ram":
        parts = [
            "ram",
            str(a["brand"]),
            str(a["series"]),
            str(a["ram_type"]),
            str(a["total_capacity"]),
            str(a["kit_config"]),
            str(a["speed"]),
        ]
        if a.get("cas_latency"):
            parts.append(str(a["cas_latency"]))
        if a.get("white"):
            parts.append("white")
        if a.get("rgb"):
            parts.append("rgb")
        return "|".join(parts).lower()

    if cat == "psu":
        parts = [
            "psu",
            str(a["brand"]),
            str(a["model"]),
            str(a["wattage"]),
            str(a["efficiency"]),
            str(a["modularity"]),
        ]
        if a.get("atx3"):
            parts.append("atx3")
        if a.get("white"):
            parts.append("white")
        return "|".join(parts).lower()

    if cat == "case":
        parts = [
            "case",
            str(a["brand"]),
            str(a["model"]),
            str(a["form_factor"]),
            str(a["colour"]),
        ]
        if a.get("argb_fans"):
            parts.append("argb-fans")
        return "|".join(parts).lower()

    if cat == "ssd":
        parts = [
            "ssd",
            str(a["brand"]),
            str(a["model"]),
            str(a["capacity"]),
            # "nvme-gen4" -> "nvme": the model pins the PCIe generation, and stores
            # that omit "Gen4" must still match stores that state it.
            "nvme" if str(a["interface"]).startswith("nvme") else str(a["interface"]),
        ]
        if a.get("heatsink"):
            parts.append("heatsink")
        return "|".join(parts).lower()

    if cat == "hdd":
        parts = [
            "hdd",
            str(a["brand"]),
            str(a["series"]),
            str(a["capacity"]),
        ]
        if a.get("rpm"):
            parts.append(str(a["rpm"]))
        return "|".join(parts).lower()

    if cat == "cooler":
        parts = [
            "cooler",
            str(a["brand"]),
            str(a["model"]),
            str(a["cooler_type"]),
        ]
        if a.get("rgb"):
            parts.append("rgb")
        if a.get("white"):
            parts.append("white")
        return "|".join(parts).lower()

    if cat == "monitor":
        parts = [
            "monitor",
            str(a["brand"]),
            str(a["model"]),
            str(a["screen_size"]),
        ]
        return "|".join(parts).lower()

    if cat in ("keyboard", "mouse", "headset", "controller"):
        parts = [cat, str(a["brand"]), str(a["model"]), str(a.get("connection") or "wired")]
        if cat == "keyboard" and a.get("switch"):
            parts.append(f"{a['switch']}-switch")
        if a.get("colourway"):
            parts.append(str(a["colourway"]))
        if a.get("white"):
            parts.append("white")
        return "|".join(parts).lower()

    if cat == "fan":
        parts = [
            "fan",
            str(a["brand"]),
            str(a["model"]),
            str(a["size_mm"]),
            str(a.get("pack_count") or "1-pack"),
        ]
        if a.get("rgb"):
            parts.append("rgb")
        if a.get("white"):
            parts.append("white")
        return "|".join(parts).lower()

    return None


def build_ram_base_signature(extraction: ExtractionResult) -> str | None:
    """Return a RAM signature excluding `cas_latency` to detect missing-CL vs keyed-CL collisions."""
    if extraction.category != "ram" or not extraction.is_valid_for_key:
        return None
    a = extraction.attributes
    parts = [
        "ram",
        str(a["brand"]),
        str(a["series"]),
        str(a["ram_type"]),
        str(a["total_capacity"]),
        str(a["kit_config"]),
        str(a["speed"]),
    ]
    if a.get("white"):
        parts.append("white")
    if a.get("rgb"):
        parts.append("rgb")
    return "|".join(parts).lower()


def _title_case_token(token: str) -> str:
    upper_tokens = {
        "amd",
        "rtx",
        "gtx",
        "gt",
        "rx",
        "xt",
        "xtx",
        "gre",
        "x3d",
        "oc",
        "rgb",
        "argb",
        "ddr3",
        "ddr4",
        "ddr5",
        "atx",
        "matx",
        "itx",
        "eatx",
        "tuf",
        "rog",
        "xpg",
        "msi",
        "pny",
        "nzxt",
        "mwe",
        "sff",
        "lp",
        "brk",
        "sl",
        "ii",
        "iii",
        "v2",
        "v3",
        "d3hp",
        "ds3h",
        "hdv",
        "vdh",
        "csm",
        "ayw",
    }
    low = token.lower()
    if low in upper_tokens:
        return low.upper()
    if low == "ti":
        return "Ti"
    if low == "wifi":
        return "WiFi"
    if re.match(r"^i[3579]$", low):
        return low
    if re.match(r"^(?:\d+x\d+gb|\d+gb|\d+w|cl\d{2})$", low):
        return low.upper()
    return low.capitalize()


def _format_slug_phrase(slug_str: str) -> str:
    return " ".join(_title_case_token(part) for part in slug_str.split("-") if part)


_SLUG_SYMBOLS = {"&": "-and-", "+": "-plus-"}


def sanitize_slug(slug: str) -> str:
    """Restrict a slug to `[a-z0-9-]` so it is safe in URLs, routes and sitemap XML.

    `&` becomes `and`, `+` becomes `plus`; any other character becomes a hyphen.
    """
    low = slug.lower()
    for sym, rep in _SLUG_SYMBOLS.items():
        low = low.replace(sym, rep)
    low = re.sub(r"[^a-z0-9-]+", "-", low)
    return re.sub(r"-{2,}", "-", low).strip("-")


def build_canonical_name_and_slug(extraction: ExtractionResult) -> tuple[str, str]:
    """Generate a clean, SEO-optimized canonical product name and immutable URL slug from attributes."""
    name, slug = _build_canonical_name_and_raw_slug(extraction)
    return name, sanitize_slug(slug)


def _build_canonical_name_and_raw_slug(extraction: ExtractionResult) -> tuple[str, str]:
    cat = extraction.category
    a = extraction.attributes
    brand_slug = str(a.get("brand") or extraction.brand_slug or "generic")
    brand_display = (
        extraction.brand_name
        or (BRAND_BY_SLUG[brand_slug].name if brand_slug in BRAND_BY_SLUG else _title_case_token(brand_slug))
    )

    if cat == "cpu":
        model_slug = str(a.get("model") or "processor")
        model_display = _format_slug_phrase(model_slug)
        # Format `Core i5 14400F` as `Core i5-14400F`
        model_display = re.sub(r"\b(Core i[3579])\s+(\d)", r"\1-\2", model_display)
        pkg = str(a.get("packaging") or "box")
        pkg_suffix = " (Tray)" if pkg == "tray" else ""
        cooler_suffix = " with Cooler" if a.get("with_cooler") else ""
        name = f"{brand_display} {model_display} Processor{pkg_suffix}{cooler_suffix}"
        slug_parts = [brand_slug, model_slug]
        if pkg == "tray":
            slug_parts.append("tray")
        if a.get("with_cooler"):
            slug_parts.append("with-cooler")
        return name, "-".join(slug_parts).lower()

    if cat == "gpu":
        chipset_slug = str(a.get("chipset") or "gpu")
        series_slug = str(a.get("series") or "standard")
        vram_slug = str(a.get("vram") or "")
        family = (
            "GeForce"
            if chipset_slug.startswith(("rtx-", "gtx-", "gt-"))
            else ("Radeon" if chipset_slug.startswith("rx-") else "")
        )
        chipset_display = _format_slug_phrase(chipset_slug)
        series_display = "" if series_slug == "standard" else _format_slug_phrase(series_slug)
        oc_str = " OC" if a.get("oc") else ""
        white_str = " White" if a.get("white") else ""
        vram_display = vram_slug.upper()

        name_tokens = [brand_display]
        if series_display:
            name_tokens.append(series_display)
        if family and family.lower() not in series_display.lower():
            name_tokens.append(family)
        name_tokens.append(f"{chipset_display}{oc_str}{white_str}")
        if vram_display:
            name_tokens.append(vram_display)
        name = " ".join(name_tokens)

        slug_tokens = [brand_slug]
        if series_slug != "standard":
            slug_tokens.append(series_slug)
        if family:
            slug_tokens.append(family.lower())
        slug_tokens.append(chipset_slug)
        if a.get("oc"):
            slug_tokens.append("oc")
        if a.get("white"):
            slug_tokens.append("white")
        if vram_slug:
            slug_tokens.append(vram_slug)
        return name, "-".join(slug_tokens).lower()

    if cat == "motherboard":
        model_slug = str(a.get("model") or "motherboard")
        mem_slug = str(a.get("memory_type") or "").upper()
        ff_slug = str(a.get("form_factor") or "").upper()
        wifi_str = " WiFi" if a.get("wifi") else ""
        white_str = " White" if a.get("white") else ""
        name = f"{brand_display} {_format_slug_phrase(model_slug)}{wifi_str}{white_str} {mem_slug} {ff_slug} Motherboard"
        slug_parts = [brand_slug, model_slug]
        if a.get("wifi"):
            slug_parts.append("wifi")
        if a.get("white"):
            slug_parts.append("white")
        if mem_slug:
            slug_parts.append(mem_slug.lower())
        if ff_slug:
            slug_parts.append(ff_slug.lower())
        return " ".join(name.split()), "-".join(slug_parts).lower()

    if cat == "ram":
        series_slug = str(a.get("series") or "memory")
        ram_type = str(a.get("ram_type") or "").upper()
        total_cap = str(a.get("total_capacity") or "").upper()
        kit = str(a.get("kit_config") or "").upper()
        speed = str(a.get("speed") or "")
        cl = str(a.get("cas_latency") or "").upper()
        rgb_str = " RGB" if a.get("rgb") and "rgb" not in series_slug else ""
        white_str = " White" if a.get("white") else ""
        cl_str = f" {cl}" if cl else ""
        series_display = "" if series_slug == "standard" else f" {_format_slug_phrase(series_slug)}"
        name = f"{brand_display}{series_display}{rgb_str} {total_cap} ({kit}) {ram_type} {speed}MHz{cl_str}{white_str} RAM"
        slug_parts = [brand_slug, series_slug, total_cap.lower(), kit.lower(), ram_type.lower(), speed]
        if cl:
            slug_parts.append(cl.lower())
        if a.get("white"):
            slug_parts.append("white")
        if a.get("rgb"):
            slug_parts.append("rgb")
        return " ".join(name.split()), "-".join(slug_parts).lower()

    if cat == "psu":
        model_slug = str(a.get("model") or "psu")
        wattage = str(a.get("wattage") or "").upper()
        eff = str(a.get("efficiency") or "").capitalize()
        mod = _format_slug_phrase(str(a.get("modularity") or ""))
        atx3_str = " ATX 3.0" if a.get("atx3") else ""
        white_str = " White" if a.get("white") else ""
        name = f"{brand_display} {model_slug.upper()} {wattage} 80+ {eff} {mod}{atx3_str}{white_str} Power Supply"
        slug_parts = [
            brand_slug,
            model_slug,
            wattage.lower(),
            eff.lower(),
            str(a.get("modularity") or "").lower(),
        ]
        if a.get("atx3"):
            slug_parts.append("atx3")
        if a.get("white"):
            slug_parts.append("white")
        return " ".join(name.split()), "-".join(slug_parts).lower()

    if cat == "ssd":
        model_slug = str(a.get("model") or "ssd")
        cap = str(a.get("capacity") or "").upper()
        iface = _format_slug_phrase(str(a.get("interface") or "nvme")).upper()
        hs_str = " with Heatsink" if a.get("heatsink") else ""
        name = f"{brand_display} {_format_slug_phrase(model_slug)} {cap} {iface} SSD{hs_str}"
        slug_parts = [brand_slug, model_slug, cap.lower(), str(a.get("interface") or "nvme").lower()]
        if a.get("heatsink"):
            slug_parts.append("heatsink")
        return " ".join(name.split()), "-".join(slug_parts).lower()

    if cat == "hdd":
        series_slug = str(a.get("series") or "hdd")
        cap = str(a.get("capacity") or "").upper()
        rpm = f" {str(a['rpm']).upper()}" if a.get("rpm") else ""
        name = f"{brand_display} {_format_slug_phrase(series_slug)} {cap}{rpm} Internal Hard Drive"
        slug_parts = [brand_slug, series_slug, cap.lower()]
        if a.get("rpm"):
            slug_parts.append(str(a["rpm"]).lower())
        return " ".join(name.split()), "-".join(slug_parts).lower()

    if cat == "cooler":
        model_slug = str(a.get("model") or "cooler")
        ctype = str(a.get("cooler_type") or "air")
        ctype_label = (
            f"{ctype.replace('aio-', '').upper()} Liquid CPU Cooler"
            if ctype.startswith("aio-")
            else "Air CPU Cooler"
        )
        rgb_str = " ARGB" if a.get("rgb") else ""
        white_str = " White" if a.get("white") else ""
        name = f"{brand_display} {_format_slug_phrase(model_slug)}{rgb_str}{white_str} {ctype_label}"
        slug_parts = [brand_slug, model_slug, ctype.lower()]
        if a.get("rgb"):
            slug_parts.append("rgb")
        if a.get("white"):
            slug_parts.append("white")
        return " ".join(name.split()), "-".join(slug_parts).lower()

    if cat == "monitor":
        model_slug = str(a.get("model") or "monitor").upper()
        size_str = str(a.get("screen_size") or "").replace("in", "-inch")
        res = str(a.get("resolution") or "").upper()
        hz = str(a.get("refresh_rate") or "")
        panel = str(a.get("panel_type") or "").upper()
        extras = " ".join(x for x in (res, hz, panel) if x)
        extra_str = f" {extras}" if extras else ""
        name = f"{brand_display} {model_slug} {size_str}{extra_str} Gaming Monitor"
        slug_parts = [brand_slug, model_slug.lower(), str(a.get("screen_size") or "").lower()]
        return " ".join(name.split()), "-".join(p for p in slug_parts if p)

    if cat in ("keyboard", "mouse", "headset", "controller"):
        from pcforge_scraper.normalize.attributes.peripherals import PERIPHERAL_NOUN

        model_slug = str(a.get("model") or cat)
        model_shown = str(a.get("model_display") or _format_slug_phrase(model_slug))
        wireless = a.get("connection") == "wireless"
        switch = str(a.get("switch") or "")
        name_bits = [brand_display, model_shown]
        if wireless and "wireless" not in model_shown.lower():
            name_bits.append("Wireless")
        name_bits.append(PERIPHERAL_NOUN[cat])
        if switch:
            name_bits.append(f"({_format_slug_phrase(switch)} Switch)")
        colourway = str(a.get("colourway") or "")
        colours = [_format_slug_phrase(colourway)] if colourway else []
        if a.get("white"):
            colours.append("White")
        if colours:
            name_bits.append(f"({' '.join(colours)})")
        slug_parts = [
            brand_slug,
            model_slug,
            "wireless" if wireless and "wireless" not in model_slug else "",
            f"{switch}-switch" if switch else "",
            colourway,
            "white" if a.get("white") else "",
            cat,
        ]
        return " ".join(" ".join(name_bits).split()), "-".join(p for p in slug_parts if p).lower()

    if cat == "fan":
        model_slug = str(a.get("model") or "fan")
        size_mm = str(a.get("size_mm") or "120mm")
        pack = str(a.get("pack_count") or "1-pack")
        rgb_str = " ARGB" if a.get("rgb") else ""
        white_str = " White" if a.get("white") else ""
        pack_str = f" ({pack.capitalize()})" if pack != "1-pack" else ""
        name = f"{brand_display} {_format_slug_phrase(model_slug)} {size_mm}{rgb_str}{white_str} Case Fan{pack_str}"
        slug_parts = [brand_slug, model_slug, size_mm.lower(), pack.lower()]
        if a.get("rgb"):
            slug_parts.append("rgb")
        if a.get("white"):
            slug_parts.append("white")
        return " ".join(name.split()), "-".join(slug_parts).lower()

    # Case
    model_slug = str(a.get("model") or "cabinet")
    ff = str(a.get("form_factor") or "atx").upper()
    colour = str(a.get("colour") or "black").capitalize()
    argb_str = " ARGB" if a.get("argb_fans") else ""
    name = f"{brand_display} {_format_slug_phrase(model_slug)}{argb_str} {ff} Cabinet ({colour})"
    slug_parts = [brand_slug, model_slug, ff.lower(), colour.lower()]
    if a.get("argb_fans"):
        slug_parts.append("argb-fans")
    return " ".join(name.split()), "-".join(slug_parts).lower()

