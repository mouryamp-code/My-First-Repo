"""Deterministic product matcher (`MPN -> KEY -> REVIEW`) with rapidfuzz near-miss hints (`Architecture.md` §5.4)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from rapidfuzz import fuzz

from pcforge_scraper.config import CategorySlug, RetailerSlug
from pcforge_scraper.normalize.attributes import ExtractionResult, extract_attributes
from pcforge_scraper.normalize.keygen import (
    build_canonical_name_and_slug,
    build_match_key,
    build_ram_base_signature,
)
from pcforge_scraper.retailers.base import ScrapedListing

MatchStatusLiteral = Literal["MATCHED", "REVIEW", "UNMATCHED"]
MatchMethodLiteral = Literal["MPN", "KEY", "MANUAL"]


# Fields a listing may omit because the model fixes them, per category.
SOFT_FIELDS: dict[str, set[str]] = {
    "psu": {"efficiency", "modularity"},
}
# Fields that must agree (when the listing states them) for a soft-field match.
IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "psu": ("brand", "model", "wattage", "efficiency", "modularity", "atx3", "white"),
}
# Boolean flags where False only means "not mentioned in the title".
LENIENT_FLAGS = frozenset({"atx3"})


@dataclass
class MatchedProduct:
    """In-memory representation of a canonical `Product` row created or matched during a run."""

    id: int
    category: CategorySlug
    brand_slug: str
    name: str
    slug: str
    match_key: str
    mpn: str | None = None
    specs: dict[str, Any] = field(default_factory=dict)
    image_url: str | None = None
    ram_base_signature: str | None = None
    retailers: set[str] = field(default_factory=set)
    listing_count: int = 0


@dataclass
class MatchOutcome:
    """Result of normalizing and matching a single `ScrapedListing`."""

    listing: ScrapedListing
    extraction: ExtractionResult
    match_status: MatchStatusLiteral
    match_method: MatchMethodLiteral | None
    match_key_candidate: str | None
    match_notes: str | None
    product: MatchedProduct | None


@dataclass
class BatchMatchReport:
    """Summary statistics from running `ProductMatcher` across a batch of listings."""

    total_listings: int
    matched_count: int
    review_count: int
    unmatched_count: int
    matched_by_mpn: int
    matched_by_key: int
    total_products: int
    multi_source_products: int
    outcomes: list[MatchOutcome]
    products: list[MatchedProduct]


def _attributes_conflict_for_mpn(
    listing_ext: ExtractionResult,
    product_specs: Mapping[str, Any],
    raw_title: str,
) -> str | None:
    """Check whether explicitly extracted title attributes contradict an MPN-matched product's specs.

    Note per `Architecture.md` §5.3:
    "A store that omits colour for a white SKU will not auto-merge; MPN handles it when present."
    So colour omission is allowed to merge via MPN unless the title explicitly states "Black" against a "White" SKU.
    """
    cat = listing_ext.category
    a = listing_ext.attributes

    if cat == "cpu":
        if a.get("model") and product_specs.get("model") and a["model"] != product_specs["model"]:
            return f"CPU model '{a['model']}' disagrees with MPN product model '{product_specs['model']}'"
        if a.get("packaging") and product_specs.get("packaging") and a["packaging"] != product_specs["packaging"]:
            return f"CPU packaging '{a['packaging']}' disagrees with MPN product '{product_specs['packaging']}'"

    elif cat == "gpu":
        if a.get("chipset") and product_specs.get("chipset") and a["chipset"] != product_specs["chipset"]:
            return f"GPU chipset '{a['chipset']}' disagrees with MPN product '{product_specs['chipset']}'"
        if a.get("vram") and product_specs.get("vram") and a["vram"] != product_specs["vram"]:
            return f"GPU VRAM '{a['vram']}' disagrees with MPN product '{product_specs['vram']}'"

    elif cat == "motherboard":
        if a.get("chipset") and product_specs.get("chipset") and a["chipset"] != product_specs["chipset"]:
            return f"Motherboard chipset '{a['chipset']}' disagrees with MPN product '{product_specs['chipset']}'"
        if (
            a.get("memory_type")
            and product_specs.get("memory_type")
            and a["memory_type"] != product_specs["memory_type"]
        ):
            return f"Motherboard memory type '{a['memory_type']}' disagrees with MPN product '{product_specs['memory_type']}'"

    elif cat == "ram":
        for field_name in ("ram_type", "total_capacity", "kit_config", "speed"):
            if a.get(field_name) and product_specs.get(field_name) and a[field_name] != product_specs[field_name]:
                return f"RAM {field_name} '{a[field_name]}' disagrees with MPN product '{product_specs[field_name]}'"
        if (
            a.get("cas_latency")
            and product_specs.get("cas_latency")
            and a["cas_latency"] != product_specs["cas_latency"]
        ):
            return f"RAM CAS latency '{a['cas_latency']}' disagrees with MPN product '{product_specs['cas_latency']}'"

    elif cat == "psu":
        if a.get("wattage") and product_specs.get("wattage") and a["wattage"] != product_specs["wattage"]:
            return f"PSU wattage '{a['wattage']}' disagrees with MPN product '{product_specs['wattage']}'"

    elif cat in ("ssd", "hdd"):
        if a.get("capacity") and product_specs.get("capacity") and a["capacity"] != product_specs["capacity"]:
            return f"Storage capacity '{a['capacity']}' disagrees with MPN product '{product_specs['capacity']}'"

    elif cat == "cooler":
        if (
            a.get("cooler_type")
            and product_specs.get("cooler_type")
            and a["cooler_type"] != product_specs["cooler_type"]
        ):
            return f"Cooler type '{a['cooler_type']}' disagrees with MPN product '{product_specs['cooler_type']}'"

    elif cat == "monitor":
        if (
            a.get("screen_size")
            and product_specs.get("screen_size")
            and a["screen_size"] != product_specs["screen_size"]
        ):
            return f"Monitor screen size '{a['screen_size']}' disagrees with MPN product '{product_specs['screen_size']}'"

    elif cat in ("keyboard", "mouse", "headset", "controller"):
        if a.get("connection") and product_specs.get("connection") and a["connection"] != product_specs["connection"]:
            return f"Connection '{a['connection']}' disagrees with MPN product '{product_specs['connection']}'"
        if a.get("switch") and product_specs.get("switch") and a["switch"] != product_specs["switch"]:
            return f"Switch '{a['switch']}' disagrees with MPN product '{product_specs['switch']}'"

    elif cat == "fan":
        if (
            a.get("pack_count")
            and product_specs.get("pack_count")
            and a["pack_count"] != product_specs["pack_count"]
        ):
            return f"Fan pack count '{a['pack_count']}' disagrees with MPN product '{product_specs['pack_count']}'"

    # Explicit colour contradiction (title explicitly says "black" when product is "white" or vice versa)
    title_low = raw_title.lower()
    prod_white = bool(product_specs.get("white"))
    if prod_white and "black" in title_low and "white" not in title_low:
        return "Title explicitly states 'black' but MPN belongs to a 'white' SKU"
    if not prod_white and "white" in title_low and "80 plus white" not in title_low and "80+ white" not in title_low:
        return "Title explicitly states 'white' but MPN belongs to a non-white SKU"

    return None


class ProductMatcher:
    """Stateful product catalog matcher implementing `MPN -> KEY -> create Product -> REVIEW`."""

    def __init__(self, existing_products: Iterable[MatchedProduct] | None = None) -> None:
        self._products: list[MatchedProduct] = []
        self._by_key: dict[str, MatchedProduct] = {}
        self._by_mpn: dict[tuple[CategorySlug, str], MatchedProduct] = {}
        self._ram_by_base_sig: dict[str, list[MatchedProduct]] = {}
        self._used_slugs: set[str] = set()
        self._next_id = 1

        for prod in existing_products or ():
            self._register_product(prod)

    @property
    def products(self) -> list[MatchedProduct]:
        """Return all canonical products in the matcher."""
        return list(self._products)

    def _register_product(self, prod: MatchedProduct) -> None:
        self._products.append(prod)
        self._by_key[prod.match_key] = prod
        self._used_slugs.add(prod.slug)
        if prod.mpn:
            self._by_mpn[(prod.category, prod.mpn.upper())] = prod
        if prod.category == "ram" and prod.ram_base_signature:
            self._ram_by_base_sig.setdefault(prod.ram_base_signature, []).append(prod)
        if prod.id >= self._next_id:
            self._next_id = prod.id + 1

    def _allocate_unique_slug(self, base_slug: str) -> str:
        if base_slug not in self._used_slugs:
            self._used_slugs.add(base_slug)
            return base_slug
        counter = 2
        while f"{base_slug}-{counter}" in self._used_slugs:
            counter += 1
        unique = f"{base_slug}-{counter}"
        self._used_slugs.add(unique)
        return unique

    def _find_near_miss_suggestion(
        self,
        category: CategorySlug,
        cleaned_title: str,
        candidate_key: str | None,
        exclude_product_id: int | None = None,
    ) -> str | None:
        """Compute rapidfuzz near-miss hint against existing products in `category` (never triggers a merge)."""
        best_score = 0.0
        best_prod: MatchedProduct | None = None
        query = candidate_key or cleaned_title

        for prod in self._products:
            if prod.category != category or prod.id == exclude_product_id:
                continue
            target = prod.match_key if candidate_key else prod.name.lower()
            score = float(fuzz.token_sort_ratio(query, target))
            if score > best_score:
                best_score = score
                best_prod = prod

        if best_prod is not None and best_score >= 72.0:
            return f"Near-miss suggestion: '{best_prod.name}' [{best_prod.match_key}] (similarity={best_score:.0f}%)"
        return None

    def match_listing(self, listing: ScrapedListing) -> MatchOutcome:
        """Normalize and match a single `ScrapedListing` against the catalog."""
        extraction = extract_attributes(
            listing.category,
            listing.raw_title,
            raw_brand=listing.raw_brand,
            raw_mpn=listing.raw_mpn,
            retailer=listing.retailer,
            source_fields=listing.source_fields,
        )
        candidate_key = build_match_key(extraction)
        ram_sig = build_ram_base_signature(extraction)

        # 0. Structured field / title conflict -> REVIEW
        if extraction.conflict_notes:
            hint = self._find_near_miss_suggestion(listing.category, extraction.cleaned_title, candidate_key)
            notes = "; ".join(extraction.conflict_notes)
            if hint:
                notes = f"{notes} | {hint}"
            return MatchOutcome(
                listing=listing,
                extraction=extraction,
                match_status="REVIEW",
                match_method=None,
                match_key_candidate=candidate_key,
                match_notes=notes,
                product=None,
            )

        # 1. MPN match (strongest signal when present)
        if extraction.mpn:
            mpn_prod = self._by_mpn.get((listing.category, extraction.mpn))
            if mpn_prod is not None:
                mpn_conflict = _attributes_conflict_for_mpn(
                    extraction, mpn_prod.specs, listing.raw_title
                )
                if mpn_conflict:
                    return MatchOutcome(
                        listing=listing,
                        extraction=extraction,
                        match_status="REVIEW",
                        match_method=None,
                        match_key_candidate=candidate_key,
                        match_notes=f"MPN conflict ({extraction.mpn}): {mpn_conflict}",
                        product=None,
                    )
                if listing.image_url and mpn_prod.image_url is None:
                    mpn_prod.image_url = listing.image_url
                mpn_prod.retailers.add(listing.retailer)
                mpn_prod.listing_count += 1
                return MatchOutcome(
                    listing=listing,
                    extraction=extraction,
                    match_status="MATCHED",
                    match_method="MPN",
                    match_key_candidate=candidate_key or mpn_prod.match_key,
                    match_notes=None,
                    product=mpn_prod,
                )

        # 2a. Only a "soft" field is missing (one the model itself determines, e.g. a PSU's
        #     modularity): match the single existing product that agrees on everything stated.
        soft_match = self._match_on_soft_fields(listing, extraction)
        if soft_match is not None:
            return soft_match

        # 2. Missing required field(s) -> REVIEW (never guess)
        if extraction.missing_fields or candidate_key is None:
            missing_str = ", ".join(extraction.missing_fields) or "unknown"
            reason = f"Missing required {listing.category} field(s): {missing_str}"
            hint = self._find_near_miss_suggestion(listing.category, extraction.cleaned_title, None)
            if hint:
                reason = f"{reason} | {hint}"
            return MatchOutcome(
                listing=listing,
                extraction=extraction,
                match_status="REVIEW",
                match_method=None,
                match_key_candidate=None,
                match_notes=reason,
                product=None,
            )

        # 3. RAM CAS latency safety check (§5.3 Note: a listing without latency is REVIEW against a keyed one)
        if listing.category == "ram" and ram_sig:
            existing_ram_group = self._ram_by_base_sig.get(ram_sig, [])
            has_cl = bool(extraction.attributes.get("cas_latency"))
            if not has_cl:
                keyed_cl_prod = next(
                    (p for p in existing_ram_group if p.specs.get("cas_latency")),
                    None,
                )
                if keyed_cl_prod is not None:
                    return MatchOutcome(
                        listing=listing,
                        extraction=extraction,
                        match_status="REVIEW",
                        match_method=None,
                        match_key_candidate=candidate_key,
                        match_notes=(
                            f"Missing RAM CAS latency in title against keyed product "
                            f"'{keyed_cl_prod.name}' [{keyed_cl_prod.match_key}]"
                        ),
                        product=None,
                    )
            else:
                unkeyed_cl_prod = next(
                    (p for p in existing_ram_group if not p.specs.get("cas_latency")),
                    None,
                )
                if unkeyed_cl_prod is not None and candidate_key not in self._by_key:
                    return MatchOutcome(
                        listing=listing,
                        extraction=extraction,
                        match_status="REVIEW",
                        match_method=None,
                        match_key_candidate=candidate_key,
                        match_notes=(
                            f"RAM CAS latency ({extraction.attributes['cas_latency']}) conflicts with "
                            f"latency-unspecified product '{unkeyed_cl_prod.name}' [{unkeyed_cl_prod.match_key}]"
                        ),
                        product=None,
                    )

        # 4. Exact match_key lookup
        existing_prod = self._by_key.get(candidate_key)
        if existing_prod is not None:
            if extraction.mpn and existing_prod.mpn is None:
                existing_prod.mpn = extraction.mpn
                self._by_mpn[(listing.category, extraction.mpn)] = existing_prod
            if listing.image_url and existing_prod.image_url is None:
                existing_prod.image_url = listing.image_url
            existing_prod.retailers.add(listing.retailer)
            existing_prod.listing_count += 1
            hint = self._find_near_miss_suggestion(
                listing.category,
                extraction.cleaned_title,
                candidate_key,
                exclude_product_id=existing_prod.id,
            )
            return MatchOutcome(
                listing=listing,
                extraction=extraction,
                match_status="MATCHED",
                match_method="KEY",
                match_key_candidate=candidate_key,
                match_notes=hint,
                product=existing_prod,
            )

        # 5. Create new Product -> MATCHED (method = KEY)
        canon_name, base_slug = build_canonical_name_and_slug(extraction)
        unique_slug = self._allocate_unique_slug(base_slug)
        assert extraction.brand_slug is not None
        new_prod = MatchedProduct(
            id=self._next_id,
            category=listing.category,
            brand_slug=extraction.brand_slug,
            name=canon_name,
            slug=unique_slug,
            match_key=candidate_key,
            mpn=extraction.mpn,
            specs=dict(extraction.attributes),
            image_url=listing.image_url,
            ram_base_signature=ram_sig,
            retailers={listing.retailer},
            listing_count=1,
        )
        self._register_product(new_prod)
        hint = self._find_near_miss_suggestion(
            listing.category,
            extraction.cleaned_title,
            candidate_key,
            exclude_product_id=new_prod.id,
        )
        return MatchOutcome(
            listing=listing,
            extraction=extraction,
            match_status="MATCHED",
            match_method="KEY",
            match_key_candidate=candidate_key,
            match_notes=hint,
            product=new_prod,
        )

    def _match_on_soft_fields(
        self, listing: ScrapedListing, extraction: ExtractionResult
    ) -> MatchOutcome | None:
        """Match a listing that lacks only model-determined fields to its unique existing product.

        Stores often leave out details that follow from the model ("Corsair RM850e 850W" is always
        80+ Gold, fully modular). If brand, model and every other stated identity field agree with
        exactly one known product, that product is the listing's product. Zero or several
        candidates stay in REVIEW.
        """
        soft = SOFT_FIELDS.get(listing.category)
        missing = set(extraction.missing_fields)
        if not soft or not missing or not missing <= soft:
            return None
        a = extraction.attributes
        if not a.get("brand") or not a.get("model"):
            return None

        identity = IDENTITY_FIELDS[listing.category]
        candidates = []
        for prod in self._products:
            if prod.category != listing.category:
                continue
            ok = True
            for field_name in identity:
                mine, theirs = a.get(field_name), prod.specs.get(field_name)
                if field_name in missing or mine is None:
                    continue
                if field_name in LENIENT_FLAGS and mine is False:
                    continue  # "ATX 3.1" not mentioned is not a contradiction
                if mine != theirs:
                    ok = False
                    break
            if ok:
                candidates.append(prod)

        if len(candidates) != 1:
            return None
        prod = candidates[0]
        if listing.image_url and prod.image_url is None:
            prod.image_url = listing.image_url
        prod.retailers.add(listing.retailer)
        prod.listing_count += 1
        return MatchOutcome(
            listing=listing,
            extraction=extraction,
            match_status="MATCHED",
            match_method="KEY",
            match_key_candidate=prod.match_key,
            match_notes=(
                f"Matched on {', '.join(sorted(f for f in identity if f not in missing))}; "
                f"{', '.join(sorted(missing))} taken from the existing product"
            ),
            product=prod,
        )

    def match_listings(self, listings: Iterable[ScrapedListing]) -> list[MatchOutcome]:
        """Match listings in order, then retry soft-field REVIEW cases once the rest are known.

        A complete listing for a product may arrive after an incomplete one from the same run;
        the second pass lets the incomplete one find it. REVIEW outcomes change no matcher state,
        so retrying them is safe.
        """
        items = list(listings)
        outcomes = [self.match_listing(item) for item in items]
        for i, (item, out) in enumerate(zip(items, outcomes, strict=True)):
            soft = SOFT_FIELDS.get(item.category)
            if out.match_status != "MATCHED" and soft and set(out.extraction.missing_fields) <= soft:
                retry = self.match_listing(item)
                if retry.match_status == "MATCHED":
                    outcomes[i] = retry
        return outcomes

    def match_batch(self, listings: Iterable[ScrapedListing]) -> BatchMatchReport:
        """Run normalization and matching across a batch of `ScrapedListing` items.

        Two-pass ordering:
        Pass 1 processes listings that have a valid MPN or CAS latency first so canonical products
        are seeded with MPNs and full keys before evaluating listings that omit MPN or CAS latency.
        """
        raw_list = list(listings)

        # Pre-extract to sort MPN-bearing and CL-bearing listings first (preserves idempotency!)
        indexed_items: list[tuple[int, ScrapedListing, int]] = []
        for idx, item in enumerate(raw_list):
            ext = extract_attributes(
                item.category,
                item.raw_title,
                raw_brand=item.raw_brand,
                raw_mpn=item.raw_mpn,
                retailer=item.retailer,
                source_fields=item.source_fields,
            )
            priority = 0
            if ext.mpn:
                priority -= 2
            if ext.attributes.get("cas_latency"):
                priority -= 1
            indexed_items.append((priority, item, idx))

        indexed_items.sort(key=lambda t: (t[0], t[2]))

        outcomes_by_idx: dict[int, MatchOutcome] = {}
        for _, item, orig_idx in indexed_items:
            outcomes_by_idx[orig_idx] = self.match_listing(item)

        ordered_outcomes = [outcomes_by_idx[i] for i in range(len(raw_list))]
        matched_count = sum(1 for o in ordered_outcomes if o.match_status == "MATCHED")
        review_count = sum(1 for o in ordered_outcomes if o.match_status == "REVIEW")
        unmatched_count = sum(1 for o in ordered_outcomes if o.match_status == "UNMATCHED")
        mpn_count = sum(1 for o in ordered_outcomes if o.match_method == "MPN")
        key_count = sum(1 for o in ordered_outcomes if o.match_method == "KEY")
        multi_source = sum(1 for p in self._products if len(p.retailers) >= 2)

        return BatchMatchReport(
            total_listings=len(ordered_outcomes),
            matched_count=matched_count,
            review_count=review_count,
            unmatched_count=unmatched_count,
            matched_by_mpn=mpn_count,
            matched_by_key=key_count,
            total_products=len(self._products),
            multi_source_products=multi_source,
            outcomes=ordered_outcomes,
            products=list(self._products),
        )


def compare_title_pair(
    category: CategorySlug,
    title_a: str,
    title_b: str,
    *,
    mpn_a: str | None = None,
    mpn_b: str | None = None,
    brand_a: str | None = None,
    brand_b: str | None = None,
    retailer_a: RetailerSlug = "vedant",
    retailer_b: RetailerSlug = "elitehubs",
    source_fields_a: Mapping[str, Any] | None = None,
    source_fields_b: Mapping[str, Any] | None = None,
) -> tuple[Literal["same", "different"], MatchOutcome, MatchOutcome]:
    """Evaluate two product listings through `ProductMatcher` and return `('same' | 'different', outcome_a, outcome_b)`."""
    listing_a = ScrapedListing(
        retailer=retailer_a,
        category=category,
        url="https://example.com/item-a",
        raw_title=title_a,
        raw_brand=brand_a,
        raw_mpn=mpn_a,
        price_paise=100000,
        source_fields=dict(source_fields_a or {}),
    )
    listing_b = ScrapedListing(
        retailer=retailer_b,
        category=category,
        url="https://example.com/item-b",
        raw_title=title_b,
        raw_brand=brand_b,
        raw_mpn=mpn_b,
        price_paise=100000,
        source_fields=dict(source_fields_b or {}),
    )
    matcher = ProductMatcher()
    report = matcher.match_batch([listing_a, listing_b])
    out_a, out_b = report.outcomes[0], report.outcomes[1]
    if (
        out_a.match_status == "MATCHED"
        and out_b.match_status == "MATCHED"
        and out_a.product is not None
        and out_b.product is not None
        and out_a.product.id == out_b.product.id
    ):
        return "same", out_a, out_b
    return "different", out_a, out_b

