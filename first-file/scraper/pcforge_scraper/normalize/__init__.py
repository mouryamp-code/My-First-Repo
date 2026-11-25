"""normalization and deterministic product matching package."""

from __future__ import annotations

from pcforge_scraper.normalize.attributes import (
    ExtractionResult,
    extract_attributes,
    infer_category_from_title,
)
from pcforge_scraper.normalize.brands import BRAND_BY_SLUG, BRAND_CATALOG, detect_brand
from pcforge_scraper.normalize.keygen import (
    build_canonical_name_and_slug,
    build_match_key,
    build_ram_base_signature,
)
from pcforge_scraper.normalize.matcher import (
    BatchMatchReport,
    MatchedProduct,
    MatchOutcome,
    ProductMatcher,
    compare_title_pair,
)
from pcforge_scraper.normalize.text import (
    clean_product_url,
    clean_store_title,
    clean_title_basic,
    extract_canonical_mpn,
    normalize_title,
    parse_price_paise,
)

__all__ = [
    "BRAND_BY_SLUG",
    "BRAND_CATALOG",
    "BatchMatchReport",
    "ExtractionResult",
    "MatchOutcome",
    "MatchedProduct",
    "ProductMatcher",
    "build_canonical_name_and_slug",
    "build_match_key",
    "build_ram_base_signature",
    "clean_product_url",
    "clean_store_title",
    "clean_title_basic",
    "compare_title_pair",
    "detect_brand",
    "extract_attributes",
    "extract_canonical_mpn",
    "infer_category_from_title",
    "normalize_title",
    "parse_price_paise",
]
