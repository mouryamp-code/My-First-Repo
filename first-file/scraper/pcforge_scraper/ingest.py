"""Full ingestion pipeline: scrape/API -> normalize & match -> Neon bulk upsert -> affiliate conversion -> revalidate."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, cast

import httpx
import psycopg

from pcforge_scraper.affiliate.cuelinks import (
    AffiliateResolution,
    CuelinksClient,
    CuelinksStartupReport,
    build_direct_retailer_affiliate_url,
    is_affiliate_stale,
)
from pcforge_scraper.config import (
    ALL_CATEGORIES,
    SCRAPER_DIR,
    CategorySlug,
    RetailerSlug,
    Settings,
    TargetsConfig,
    get_settings,
    load_targets,
)
from pcforge_scraper.db import (
    record_price_snapshots,
    BULK_CHUNK_SIZE,
    ConnectionKeepAlive,
    DatabaseReport,
    ListingUpsertItem,
    ProductUpsertItem,
    RunStatusType,
    UpsertedListingRow,
    batch_update_listing_affiliates,
    bulk_upsert_listings,
    bulk_upsert_products,
    connect_db,
    ensure_reference_data,
    fetch_database_report,
    finish_scrape_run,
    insert_scrape_run,
    load_existing_products,
    load_known_amazon_asins,
    refresh_product_rollups,
    upsert_listing,
    upsert_product,
)
from pcforge_scraper.normalize.matcher import MatchOutcome, ProductMatcher
from pcforge_scraper.retailers import (
    SCRAPER_CLASSES,
    RetailerSource,
    ScrapedListing,
    get_retailer_source,
)

logger = logging.getLogger(__name__)


@dataclass
class RetailerIngestionOutcome:
    """Per-retailer ingestion summary."""

    retailer: RetailerSlug
    run_id: int
    status: RunStatusType
    items_found: int = 0
    items_ok: int = 0
    items_failed: int = 0
    matched_count: int = 0
    review_count: int = 0
    unmatched_count: int = 0
    affiliated_count: int = 0
    plain_count: int = 0
    error: Optional[str] = None
    listings: List[ScrapedListing] = field(default_factory=list)


@dataclass
class IngestionPipelineResult:
    """Full pipeline result returned by `run_ingestion_pipeline`."""

    retailer_outcomes: List[RetailerIngestionOutcome]
    cuelinks_startup: CuelinksStartupReport
    database_report: DatabaseReport
    revalidated: bool = False


def trigger_web_revalidation(settings: Optional[Settings] = None) -> bool:
    """Optionally ping `POST {NEXT_PUBLIC_SITE_URL}/api/revalidate` when `REVALIDATE_SECRET` is set."""
    cfg = settings or get_settings()
    if cfg.revalidate_secret is None:
        return False
    secret = cfg.revalidate_secret.get_secret_value().strip()
    if not secret:
        return False

    site_url = cfg.next_public_site_url.rstrip("/")
    url = f"{site_url}/api/revalidate"
    try:
        resp = httpx.post(
            url,
            headers={"x-revalidate-secret": secret},
            json={"secret": secret},
            timeout=10.0,
        )
        if resp.status_code < 400:
            logger.info("[revalidate] Triggered web cache revalidation at %s", url)
            return True
        logger.warning(
            "[revalidate] POST %s returned HTTP %d (non-fatal)",
            url,
            resp.status_code,
        )
    except Exception as exc:
        logger.warning("[revalidate] Could not reach %s (non-fatal): %s", url, exc)
    return False


def ingest_retailer(
    conn: psycopg.Connection[object],
    *,
    retailer: RetailerSlug,
    categories: Sequence[CategorySlug],
    targets: TargetsConfig,
    matcher: ProductMatcher,
    cuelinks_client: CuelinksClient,
    retailer_id: int,
    category_ids: dict[str, int],
    brand_ids: dict[str, int],
    fixtures_root: Optional[Path] = None,
    settings: Optional[Settings] = None,
    source: Optional[RetailerSource] = None,
) -> RetailerIngestionOutcome:
    """Scrape, match, bulk-upsert, convert affiliate links, and refresh product rollups for one retailer.

    Pass `source` to ingest listings gathered elsewhere (e.g. the targeted lookup job) through the
    exact same matching, upsert, affiliate and rollup steps.
    """
    cfg = settings or get_settings()
    run_id = insert_scrape_run(conn, retailer_id)
    outcome = RetailerIngestionOutcome(
        retailer=retailer,
        run_id=run_id,
        status="RUNNING",
    )

    category_errors: List[str] = []
    if source is None:
        source = get_retailer_source(
            retailer,
            targets=targets,
            fixtures_root=fixtures_root,
        )
    # Amazon: hand over the listings we already know so they get re-checked by ASIN (cheap).
    if retailer == "amazon" and hasattr(source, "known_asins") and cfg.amazon_refresh_known:
        try:
            source.known_asins = load_known_amazon_asins(conn, retailer_id, list(categories))
            logger.info(
                "[amazon] Known listings to refresh: %s",
                {cat: len(asins) for cat, asins in source.known_asins.items()},
            )
        except Exception as exc:  # never block the run on this
            logger.warning("[amazon] Could not load known ASINs: %s", exc)
    # Fetching can take many minutes (searching only, no database writes); keep the database
    # connection alive meanwhile so the writes below don't hit a dropped connection.
    keepalive = ConnectionKeepAlive(conn).start()
    try:
        for cat in categories:
            if not source.supports(cat):
                continue
            quota = targets.get_quota(cat)
            seeds = targets.get_seeds(cat)
            try:
                items = source.fetch_category(cat, quota=quota, seeds=seeds)
                outcome.listings.extend(items)
                outcome.items_found += len(items)
                # Adapters swallow per-page failures (Cloudflare, robots, HTTP errors) into notes;
                # record them when a category comes back empty so the run says why.
                note = source.last_category_notes.get(cat)
                if not items and note:
                    logger.warning("[%s] %s returned no listings: %s", retailer, cat, note)
                    category_errors.append(f"{cat}: {note}")
            except Exception as exc:
                err_msg = f"{cat}: {exc}"
                logger.error("[%s] Failed fetching category %s: %s", retailer, cat, exc)
                category_errors.append(err_msg)
                outcome.items_failed += 1
    finally:
        keepalive.stop()
        close_fn = getattr(source, "close", None)
        if callable(close_fn):
            close_fn()

    # Step 1: Normalize and match all listings in memory, preparing bulk product upserts
    matched_pairs: List[Tuple[ScrapedListing, MatchOutcome]] = []
    product_upsert_items: List[ProductUpsertItem] = []

    for listing, match_outcome in zip(outcome.listings, matcher.match_listings(outcome.listings), strict=True):
        matched_pairs.append((listing, match_outcome))
        if (
            match_outcome.match_status == "MATCHED"
            and match_outcome.product is not None
            and match_outcome.extraction.brand_slug is not None
        ):
            brand_id = brand_ids.get(match_outcome.extraction.brand_slug)
            cat_id = category_ids.get(listing.category)
            if brand_id is not None and cat_id is not None:
                product_upsert_items.append(
                    ProductUpsertItem(
                        product=match_outcome.product,
                        category_id=cat_id,
                        brand_id=brand_id,
                    )
                )

    # Step 2: Bulk upsert canonical products in chunked psycopg batches
    product_ids_by_key: dict[str, int] = {}
    if product_upsert_items:
        try:
            logger.info(
                "[%s] Executing bulk product upsert (%d matched product refs, chunk_size=%d)...",
                retailer,
                len(product_upsert_items),
                BULK_CHUNK_SIZE,
            )
            product_ids_by_key = bulk_upsert_products(
                conn,
                product_upsert_items,
                chunk_size=BULK_CHUNK_SIZE,
            )
        except Exception as exc:
            conn.rollback()
            logger.warning(
                "[%s] Bulk product upsert encountered an error (%s); falling back to row-by-row product upsert",
                retailer,
                exc,
            )
            for p_item in product_upsert_items:
                try:
                    pid = upsert_product(
                        conn,
                        p_item.product,
                        category_id=p_item.category_id,
                        brand_id=p_item.brand_id,
                    )
                    product_ids_by_key[p_item.product.match_key] = pid
                except Exception as row_exc:
                    conn.rollback()
                    category_errors.append(f"product({p_item.product.match_key}): {row_exc}")

    # Step 3: Prepare and execute bulk listing upsert
    listing_upsert_items: List[ListingUpsertItem] = []
    for listing, match_outcome in matched_pairs:
        cat_id = category_ids[listing.category]
        db_product_id: Optional[int] = None
        if match_outcome.match_status == "MATCHED" and match_outcome.product is not None:
            db_product_id = product_ids_by_key.get(
                match_outcome.product.match_key,
                match_outcome.product.id,
            )
        listing_upsert_items.append(
            ListingUpsertItem(
                listing=listing,
                outcome=match_outcome,
                retailer_id=retailer_id,
                category_id=cat_id,
                product_id=db_product_id,
            )
        )

    upserted_rows: List[Tuple[ListingUpsertItem, UpsertedListingRow]] = []
    if listing_upsert_items:
        try:
            logger.info(
                "[%s] Executing bulk listing upsert (%d listings, chunk_size=%d)...",
                retailer,
                len(listing_upsert_items),
                BULK_CHUNK_SIZE,
            )
            bulk_rows = bulk_upsert_listings(
                conn,
                listing_upsert_items,
                chunk_size=BULK_CHUNK_SIZE,
            )
            upserted_rows = list(zip(listing_upsert_items, bulk_rows, strict=True))
        except Exception as exc:
            conn.rollback()
            logger.warning(
                "[%s] Bulk listing upsert encountered an error (%s); falling back to row-by-row listing upsert",
                retailer,
                exc,
            )
            for l_item in listing_upsert_items:
                try:
                    u_row = upsert_listing(
                        conn,
                        l_item.listing,
                        l_item.outcome,
                        retailer_id=l_item.retailer_id,
                        category_id=l_item.category_id,
                        product_id=l_item.product_id,
                    )
                    upserted_rows.append((l_item, u_row))
                except Exception as row_exc:
                    conn.rollback()
                    logger.error("[%s] Failed upserting listing %s: %s", retailer, l_item.listing.url, row_exc)
                    category_errors.append(f"upsert({l_item.listing.url}): {row_exc}")
                    outcome.items_failed += 1

    # Step 4: Resolve affiliate links and batch-update in chunks
    pending_affiliate_updates: List[Tuple[int, AffiliateResolution]] = []
    for l_item, upserted in upserted_rows:
        listing = l_item.listing
        match_outcome = l_item.outcome
        db_product_id = l_item.product_id

        if match_outcome.match_status == "MATCHED" and db_product_id is not None:
            outcome.matched_count += 1
            direct_candidate = build_direct_retailer_affiliate_url(
                retailer,
                listing.url,
                cfg,
            )
            needs_affiliate_refresh = is_affiliate_stale(
                upserted.affiliate_url,
                upserted.affiliate_checked_at,
            ) or (
                direct_candidate is not None
                and upserted.affiliate_url != direct_candidate.affiliate_url
                and upserted.affiliate_provider != "CUELINKS"
            )

            if needs_affiliate_refresh:
                resolution = cuelinks_client.resolve_listing_affiliate_link(
                    retailer=retailer,
                    clean_url=listing.url,
                    product_id=db_product_id,
                    category=listing.category,
                )
                pending_affiliate_updates.append((upserted.listing_id, resolution))
                is_affiliated = bool(resolution.affiliated and resolution.affiliate_url)
            else:
                is_affiliated = bool(upserted.affiliated and upserted.affiliate_url)

            if is_affiliated:
                outcome.affiliated_count += 1
            else:
                outcome.plain_count += 1
        elif match_outcome.match_status == "REVIEW":
            outcome.review_count += 1
            direct_candidate = (
                build_direct_retailer_affiliate_url(retailer, listing.url, cfg)
                if cfg.retailer_affiliate_preference.get(retailer) == "direct_then_cuelinks"
                else None
            )
            if direct_candidate is not None and direct_candidate.affiliate_url:
                if upserted.affiliate_url != direct_candidate.affiliate_url:
                    pending_affiliate_updates.append((upserted.listing_id, direct_candidate))
                outcome.affiliated_count += 1
            else:
                outcome.plain_count += 1
        else:
            outcome.unmatched_count += 1
            outcome.plain_count += 1

        outcome.items_ok += 1

    if pending_affiliate_updates:
        batch_update_listing_affiliates(conn, pending_affiliate_updates, chunk_size=BULK_CHUNK_SIZE)
    conn.commit()

    # Refresh product.best_price_paise and product.store_count after each retailer
    refresh_product_rollups(conn)
    # Daily price history for charts and price alerts (no-op before the web migration).
    try:
        record_price_snapshots(conn)
    except psycopg.Error as exc:  # never fail a scrape because of history
        conn.rollback()
        logger.warning("Price snapshot failed: %s", exc)

    if outcome.items_ok > 0 and outcome.items_failed == 0:
        final_status: RunStatusType = "SUCCESS"
    elif outcome.items_ok > 0 and outcome.items_failed > 0:
        final_status = "PARTIAL"
    elif outcome.items_found == 0 and not category_errors:
        final_status = "PARTIAL"
    else:
        final_status = "FAILED"

    outcome.status = final_status
    outcome.error = "; ".join(category_errors[:5]) if category_errors else None
    finish_scrape_run(
        conn,
        run_id,
        status=final_status,
        items_found=outcome.items_found,
        items_ok=outcome.items_ok,
        items_failed=outcome.items_failed,
        error=outcome.error,
    )
    return outcome


def run_ingestion_pipeline(
    *,
    retailers: Optional[Sequence[RetailerSlug]] = None,
    categories: Optional[Sequence[CategorySlug]] = None,
    use_fixtures: bool = False,
    settings: Optional[Settings] = None,
    conn: Optional[psycopg.Connection[object]] = None,
) -> IngestionPipelineResult:
    """Execute the full Phase 5 ingestion pipeline across requested retailers and categories."""
    cfg = settings or get_settings()
    targets = load_targets()
    target_retailers: List[RetailerSlug] = (
        list(retailers)
        if retailers is not None
        else [cast(RetailerSlug, r) for r in SCRAPER_CLASSES]
    )
    target_categories: List[CategorySlug] = (
        list(categories) if categories is not None else list(ALL_CATEGORIES)
    )
    fixtures_root = (SCRAPER_DIR / "tests" / "fixtures") if use_fixtures else None
    cuelinks_fixtures = (fixtures_root / "cuelinks") if fixtures_root is not None else None

    def _run_with_conn(db_conn: psycopg.Connection[object]) -> IngestionPipelineResult:
        refs = ensure_reference_data(db_conn)
        existing_products = load_existing_products(db_conn)
        matcher = ProductMatcher(existing_products=existing_products)

        with CuelinksClient(
            settings=cfg,
            fixtures_dir=cuelinks_fixtures,
        ) as cuelinks_client:
            startup_report = cuelinks_client.startup_check(retailers=target_retailers)
            outcomes: List[RetailerIngestionOutcome] = []

            for r_slug in target_retailers:
                retailer_id = refs.retailers[r_slug]
                r_outcome = ingest_retailer(
                    db_conn,
                    retailer=r_slug,
                    categories=target_categories,
                    targets=targets,
                    matcher=matcher,
                    cuelinks_client=cuelinks_client,
                    retailer_id=retailer_id,
                    category_ids=refs.categories,
                    brand_ids=refs.brands,
                    fixtures_root=fixtures_root,
                    settings=cfg,
                )
                outcomes.append(r_outcome)

        revalidated = trigger_web_revalidation(cfg)
        db_report = fetch_database_report(db_conn)
        return IngestionPipelineResult(
            retailer_outcomes=outcomes,
            cuelinks_startup=startup_report,
            database_report=db_report,
            revalidated=revalidated,
        )

    if conn is not None:
        return _run_with_conn(conn)

    with connect_db(cfg) as live_conn:
        return _run_with_conn(live_conn)
