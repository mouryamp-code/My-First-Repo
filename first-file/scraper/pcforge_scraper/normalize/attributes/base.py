"""Base types and shared conflict checks for per-category attribute extractors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pcforge_scraper.config import CategorySlug


@dataclass
class ExtractionResult:
    """Output of a category attribute extractor prior to key generation and matching."""

    category: CategorySlug
    cleaned_title: str
    brand_slug: str | None
    brand_name: str | None
    mpn: str | None
    attributes: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    conflict_notes: list[str] = field(default_factory=list)

    @property
    def is_valid_for_key(self) -> bool:
        """Return True if all required attributes are present and no evidence conflicts exist."""
        return not self.missing_fields and not self.conflict_notes


def get_structured_field(source_fields: Mapping[str, Any] | None, *keys: str) -> str | None:
    """Read a non-empty string field from `source_fields` or `source_fields['attributes']`."""
    if not source_fields:
        return None
    for k in keys:
        val = source_fields.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    attrs = source_fields.get("attributes")
    if isinstance(attrs, Mapping):
        for k in keys:
            val = attrs.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def check_colour_conflict(
    raw_title: str,
    source_fields: Mapping[str, Any] | None,
) -> str | None:
    """Return a conflict note if `raw_title` and `source_fields` explicitly state opposing colours (black vs white)."""
    sf_color_raw = get_structured_field(source_fields, "color", "colour")
    if not sf_color_raw:
        return None
    sf_lower = sf_color_raw.lower()
    sf_color: str | None = None
    if "white" in sf_lower or "ice" in sf_lower or "snow" in sf_lower:
        sf_color = "white"
    elif "black" in sf_lower:
        sf_color = "black"

    if sf_color is None:
        return None

    # Remove "80 plus white" / "80+ white" before checking title colour
    title_clean = re.sub(r"\b80\s*(?:\+|plus)\s*white\b", "", raw_title.lower())
    title_has_white = bool(re.search(r"\b(?:white|ice|snow)\b", title_clean))
    title_has_black = bool(re.search(r"\bblack\b", title_clean))

    if title_has_white and not title_has_black and sf_color == "black":
        return f"Colour conflict: structured field says '{sf_color_raw}' but title specifies 'white'"
    if title_has_black and not title_has_white and sf_color == "white":
        return f"Colour conflict: structured field says '{sf_color_raw}' but title specifies 'black'"
    return None


def check_capacity_conflict(
    extracted_gb: str | None,
    source_fields: Mapping[str, Any] | None,
    field_label: str = "capacity",
) -> str | None:
    """Return a conflict note if `source_fields` specifies a GB capacity that disagrees with `extracted_gb`."""
    if not extracted_gb or not source_fields:
        return None
    for key in ("storage", "size", "capacity", "vram", "memory_size"):
        raw_val = get_structured_field(source_fields, key)
        if not raw_val:
            continue
        m = re.search(r"\b(\d{1,3})\s*gb\b", raw_val.lower())
        if m:
            sf_gb = f"{m.group(1)}gb"
            if sf_gb != extracted_gb:
                return (
                    f"{field_label.capitalize()} conflict: structured field '{key}={raw_val}' "
                    f"disagrees with title '{extracted_gb}'"
                )
    return None

