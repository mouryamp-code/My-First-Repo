"""PostgreSQL repository, connection lifecycle manager, and bulk SQL upserts for (psycopg 3)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Literal, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from pcforge_scraper.affiliate.cuelinks import AffiliateResolution
from pcforge_scraper.config import AMAZON_PRICE_MAX_AGE_HOURS, CategorySlug, Settings, get_settings
from pcforge_scraper.normalize.brands import BRAND_CATALOG
from pcforge_scraper.normalize.matcher import MatchedProduct, MatchOutcome
from pcforge_scraper.retailers.base import ScrapedListing

logger = logging.getLogger(__name__)

RunStatusType = Literal["RUNNING", "SUCCESS", "PARTIAL", "FAILED"]

BULK_CHUNK_SIZE: int = 50


@dataclass(frozen=True)
class ReferenceIds:
    """Cached primary keys for seeded `category`, `brand`, and `retailer` rows."""

    categories: dict[str, int]
    brands: dict[str, int]
    retailers: dict[str, int]


@dataclass(frozen=True)
class ProductUpsertItem:
    """Input payload for bulk product upsert."""

    product: MatchedProduct
    category_id: int
    brand_id: int


@dataclass(frozen=True)
class ListingUpsertItem:
    """Input payload for bulk listing upsert."""

    listing: ScrapedListing
    outcome: MatchOutcome
    retailer_id: int
    category_id: int
    product_id: Optional[int]


@dataclass(frozen=True)
class UpsertedListingRow:
    """Metadata returned by `upsert_listing` / `bulk_upsert_listings` for affiliate link checking."""

    listing_id: int
    affiliate_url: Optional[str]
    affiliated: bool
    affiliate_provider: Optional[str]
    affiliate_checked_at: Optional[datetime]


@dataclass(frozen=True)
class RetailerRunSummaryRow:
    """Latest `scrape_run` row per retailer for `pcforge-scrape report`."""

    retailer_slug: str
    retailer_name: str
    run_id: Optional[int]
    status: Optional[str]
    items_found: int
    items_ok: int
    items_failed: int
    error: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


@dataclass(frozen=True)
class RetailerListingStatsRow:
    """Per-retailer listing match and affiliate counts for `pcforge-scrape report`."""

    retailer_slug: str
    retailer_name: str
    total_listings: int
    matched_count: int
    review_count: int
    unmatched_count: int
    affiliated_count: int
    plain_count: int


@dataclass(frozen=True)
class DatabaseReport:
    """Full database ingestion & affiliate report returned by `fetch_database_report`."""

    last_runs: List[RetailerRunSummaryRow]
    retailer_stats: List[RetailerListingStatsRow]
    total_products: int
    multi_store_products: int
    priced_products: int


def _chunked(seq: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """Yield successive slices of `seq` of length up to `size`."""
    step = max(1, size)
    for idx in range(0, len(seq), step):
        yield seq[idx : idx + step]


def get_database_dsn(settings: Optional[Settings] = None, *, prefer_pooled: bool = True) -> str:
    """Return the PostgreSQL connection string from `DATABASE_URL` (pooled) or `DIRECT_URL`."""
    cfg = settings or get_settings()
    if prefer_pooled:
        if cfg.database_url is not None and cfg.database_url.get_secret_value().strip():
            return cfg.database_url.get_secret_value().strip()
        if cfg.direct_url is not None and cfg.direct_url.get_secret_value().strip():
            return cfg.direct_url.get_secret_value().strip()
    else:
        if cfg.direct_url is not None and cfg.direct_url.get_secret_value().strip():
            return cfg.direct_url.get_secret_value().strip()
        if cfg.database_url is not None and cfg.database_url.get_secret_value().strip():
            return cfg.database_url.get_secret_value().strip()
    raise RuntimeError("Neither DATABASE_URL nor DIRECT_URL is configured in .env")


@contextmanager
def connect_db(
    settings: Optional[Settings] = None,
    *,
    prefer_pooled: bool = True,
) -> Iterator[psycopg.Connection[Any]]:
    """Open and cleanly terminate a psycopg 3 connection to Neon PostgreSQL.

    Ensures all connections terminate deterministically upon completion of a batch run
    so staggered cron workers never exhaust Neon's connection pool.
    """
    dsn = get_database_dsn(settings, prefer_pooled=prefer_pooled)
    logger.info("Opening active database pool connection to Neon PostgreSQL...")
    conn = psycopg.connect(
        dsn,
        connect_timeout=15,
        application_name="pcforge-scraper",
        autocommit=False,
    )
    try:
        yield conn
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception as rb_exc:
                logger.warning("Database rollback encountered an error: %s", rb_exc)
        raise
    finally:
        if not conn.closed:
            logger.info("Closing active database pool connection cleanly")
            conn.close()


CATEGORY_SEED_ROWS: Tuple[Tuple[str, str], ...] = (
    ("cpu", "Processors (CPU)"),
    ("gpu", "Graphics Cards (GPU)"),
    ("motherboard", "Motherboards"),
    ("ram", "Memory (RAM)"),
    ("psu", "Power Supplies (PSU)"),
    ("case", "Cabinets (PC Cases)"),
    ("ssd", "Solid State Drives (SSD)"),
    ("hdd", "Hard Disk Drives (HDD)"),
    ("cooler", "CPU Coolers (Air & AIO)"),
    ("monitor", "Monitors"),
    ("fan", "Case Fans"),
    ("keyboard", "Keyboards"),
    ("mouse", "Gaming Mice"),
    ("headset", "Headsets & Headphones"),
    ("controller", "Game Controllers"),
)

# Stores read through the shared platform adapters (`retailers/stores.py`): (slug, name, base_url).
PLATFORM_RETAILER_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("pcstudio", "PC Studio", "https://www.pcstudio.in"),
    ("primeabgb", "PrimeABGB", "https://www.primeabgb.com"),
    ("sclgaming", "SCL Gaming", "https://sclgaming.in"),
    ("tpstech", "TPS Technologies", "https://www.tpstech.in"),
    ("vishal", "Vishal Peripherals", "https://vishalperipherals.com"),
)


def _amazon_credentials_usable(settings: Settings) -> bool:
    """True when Creators API credentials are set and the secret is plaintext (not a SIEGE blob)."""
    if not (settings.amazon_creators_client_id and settings.amazon_partner_tag):
        return False
    secret = settings.amazon_creators_client_secret
    value = secret.get_secret_value().strip() if secret else ""
    return bool(value) and not value.startswith("?SIEGE|")


def ensure_reference_data(conn: psycopg.Connection[Any]) -> ReferenceIds:
    """Sync `CATEGORY_SEED_ROWS` and `BRAND_CATALOG` into PostgreSQL and return `{slug: id}` maps."""
    with conn.cursor() as cur:
        cur.execute("ALTER TYPE \"AffiliateProvider\" ADD VALUE IF NOT EXISTS 'VEDANT_AFFILIATE'")
        cur.execute("ALTER TYPE \"AffiliateProvider\" ADD VALUE IF NOT EXISTS 'COMPUTECH_AFFILIATE'")
        cur.executemany(
            """
            INSERT INTO category (slug, name)
            VALUES (%s, %s)
            ON CONFLICT (slug) DO UPDATE SET
              name = EXCLUDED.name
            """,
            CATEGORY_SEED_ROWS,
        )
        cur.execute(
            """
            INSERT INTO retailer (slug, name, base_url, source_type, max_age_hours, is_active)
            VALUES ('computech', 'Computech Store', 'https://computechstore.in', 'SCRAPER'::"SourceType", 48, TRUE)
            ON CONFLICT (slug) DO UPDATE SET
              name = EXCLUDED.name,
              base_url = EXCLUDED.base_url,
              source_type = EXCLUDED.source_type,
              max_age_hours = EXCLUDED.max_age_hours,
              is_active = EXCLUDED.is_active
            """
        )
        cur.executemany(
            """
            INSERT INTO retailer (slug, name, base_url, source_type, max_age_hours, is_active)
            VALUES (%s, %s, %s, 'SCRAPER'::"SourceType", 48, TRUE)
            ON CONFLICT (slug) DO UPDATE SET
              name = EXCLUDED.name,
              base_url = EXCLUDED.base_url
            """,
            PLATFORM_RETAILER_ROWS,
        )
        if _amazon_credentials_usable(get_settings()):
            # Seeded inactive until Creators API access exists; every web query filters on
            # `is_active`, so Amazon listings stay invisible until this flips.
            cur.execute("UPDATE retailer SET is_active = TRUE WHERE slug = 'amazon' AND NOT is_active")
        cur.executemany(
            """
            INSERT INTO brand (slug, name, aliases)
            VALUES (%s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
              name = EXCLUDED.name,
              aliases = EXCLUDED.aliases
            """,
            [(entry.slug, entry.name, list(entry.aliases)) for entry in BRAND_CATALOG],
        )

        cur.execute("SELECT id, slug FROM category")
        categories = {str(slug): int(cid) for cid, slug in cur.fetchall()}

        cur.execute("SELECT id, slug FROM brand")
        brands = {str(slug): int(bid) for bid, slug in cur.fetchall()}

        cur.execute("SELECT id, slug FROM retailer")
        retailers = {str(slug): int(rid) for rid, slug in cur.fetchall()}

    conn.commit()
    return ReferenceIds(categories=categories, brands=brands, retailers=retailers)


def load_existing_products(conn: psycopg.Connection[Any]) -> List[MatchedProduct]:
    """Load all existing `product` rows from PostgreSQL so `ProductMatcher` preserves slugs and IDs."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              p.id AS product_id,
              c.slug AS category_slug,
              b.slug AS brand_slug,
              b.name AS brand_name,
              p.name,
              p.slug,
              p.match_key,
              p.mpn,
              p.attributes,
              p.specs,
              p.image_url
            FROM product p
            JOIN category c ON c.id = p.category_id
            JOIN brand b ON b.id = p.brand_id
            ORDER BY p.id ASC
            """
        )
        rows = cur.fetchall()

    products: List[MatchedProduct] = []
    for row in rows:
        cat_slug: CategorySlug = row["category_slug"]
        attrs = row["attributes"] if isinstance(row["attributes"], dict) else {}
        specs = row["specs"] if isinstance(row["specs"], dict) else {}
        merged_specs = {**specs, **attrs}
        ram_sig: Optional[str] = None
        if (
            cat_slug == "ram"
            and merged_specs.get("ram_type")
            and merged_specs.get("kit_config")
            and merged_specs.get("speed_mhz")
        ):
            ram_sig = (
                f"ram:{row['brand_slug']}:{merged_specs['ram_type']}:"
                f"{merged_specs['kit_config']}:{merged_specs['speed_mhz']}"
            )
        products.append(
            MatchedProduct(
                id=int(row["product_id"]),
                category=cat_slug,
                brand_slug=str(row["brand_slug"]),
                name=str(row["name"]),
                slug=str(row["slug"]),
                match_key=str(row["match_key"]),
                mpn=str(row["mpn"]) if row["mpn"] else None,
                specs=merged_specs,
                image_url=str(row["image_url"]) if row["image_url"] else None,
                ram_base_signature=ram_sig,
            )
        )
    return products


def insert_scrape_run(conn: psycopg.Connection[Any], retailer_id: int) -> int:
    """Insert a new `scrape_run` row with `status = 'RUNNING'` and return its `id`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scrape_run (
              retailer_id, status, items_found, items_ok, items_failed, started_at
            )
            VALUES (%s, 'RUNNING'::"RunStatus", 0, 0, 0, now())
            RETURNING id
            """,
            (retailer_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to insert scrape_run row")
        run_id = int(row[0])
    conn.commit()
    return run_id


def finish_scrape_run(
    conn: psycopg.Connection[Any],
    run_id: int,
    *,
    status: RunStatusType,
    items_found: int,
    items_ok: int,
    items_failed: int,
    error: Optional[str] = None,
) -> None:
    """Update a `scrape_run` row with final counts, status, and `finished_at = now()`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scrape_run
            SET status = %s::"RunStatus",
                items_found = %s,
                items_ok = %s,
                items_failed = %s,
                error = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, items_found, items_ok, items_failed, error, run_id),
        )
    conn.commit()


def bulk_upsert_products(
    conn: psycopg.Connection[Any],
    items: Sequence[ProductUpsertItem],
    *,
    chunk_size: int = BULK_CHUNK_SIZE,
) -> dict[str, int]:
    """Bulk-upsert canonical `product` rows in chunked `executemany(..., returning=True)` batches.

    Never overwrites existing `product.slug` or `product.name` on conflict.
    Returns `{match_key: db_product_id}` and updates `.id` and `.slug` on all passed `MatchedProduct` objects.
    """
    if not items:
        return {}

    unique_by_key: dict[str, ProductUpsertItem] = {}
    all_by_key: dict[str, List[MatchedProduct]] = {}
    for item in items:
        mkey = item.product.match_key
        if mkey not in unique_by_key:
            unique_by_key[mkey] = item
        all_by_key.setdefault(mkey, []).append(item.product)

    deduped_items = list(unique_by_key.values())
    resolved_ids: dict[str, int] = {}

    sql = """
        INSERT INTO product (
          category_id, brand_id, name, slug, match_key, mpn,
          attributes, specs, image_url, created_at, updated_at
        )
        VALUES (
          %s, %s, %s, %s, %s, %s,
          %s::jsonb, %s::jsonb, %s, now(), now()
        )
        ON CONFLICT (match_key) DO UPDATE SET
          mpn = COALESCE(product.mpn, EXCLUDED.mpn),
          image_url = COALESCE(product.image_url, EXCLUDED.image_url),
          specs = product.specs || EXCLUDED.specs,
          updated_at = now()
        RETURNING id, slug, match_key
    """

    for chunk in _chunked(deduped_items, chunk_size):
        params_seq = [
            (
                it.category_id,
                it.brand_id,
                it.product.name,
                it.product.slug,
                it.product.match_key,
                it.product.mpn,
                json.dumps(it.product.specs),
                json.dumps(it.product.specs),
                it.product.image_url,
            )
            for it in chunk
        ]
        with conn.cursor() as cur:
            cur.executemany(sql, params_seq, returning=True)
            while True:
                row = cur.fetchone()
                if row is not None:
                    db_id = int(row[0])
                    db_slug = str(row[1])
                    mkey = str(row[2])
                    resolved_ids[mkey] = db_id
                    for prod_obj in all_by_key.get(mkey, []):
                        prod_obj.id = db_id
                        prod_obj.slug = db_slug
                if not cur.nextset():
                    break
        conn.commit()

    return resolved_ids


def upsert_product(
    conn: psycopg.Connection[Any],
    product: MatchedProduct,
    *,
    category_id: int,
    brand_id: int,
) -> int:
    """Upsert a single canonical `product` on `match_key` without overwriting `slug` or `name`."""
    resolved = bulk_upsert_products(
        conn,
        [ProductUpsertItem(product=product, category_id=category_id, brand_id=brand_id)],
        chunk_size=1,
    )
    db_id = resolved.get(product.match_key)
    if db_id is None:
        raise RuntimeError(f"Failed to upsert product for match_key={product.match_key}")
    return db_id


def bulk_upsert_listings(
    conn: psycopg.Connection[Any],
    items: Sequence[ListingUpsertItem],
    *,
    chunk_size: int = BULK_CHUNK_SIZE,
) -> List[UpsertedListingRow]:
    """Bulk-upsert `listing` rows on `(retailer_id, url)` in chunked `executemany(..., returning=True)` batches."""
    if not items:
        return []

    sql = """
        INSERT INTO listing (
          retailer_id, product_id, category_id, url, external_id,
          raw_title, raw_brand, raw_mpn, price_paise, mrp_paise,
          in_stock, image_url, raw_attributes, match_status, match_method,
          match_key_candidate, match_notes, first_seen_at, last_seen_at
        )
        VALUES (
          %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s,
          %s, %s, %s::jsonb, %s::"MatchStatus", %s::"MatchMethod",
          %s, %s, now(), now()
        )
        ON CONFLICT (retailer_id, url) DO UPDATE SET
          product_id = EXCLUDED.product_id,
          category_id = EXCLUDED.category_id,
          external_id = COALESCE(EXCLUDED.external_id, listing.external_id),
          raw_title = EXCLUDED.raw_title,
          raw_brand = COALESCE(EXCLUDED.raw_brand, listing.raw_brand),
          raw_mpn = COALESCE(EXCLUDED.raw_mpn, listing.raw_mpn),
          price_paise = EXCLUDED.price_paise,
          mrp_paise = EXCLUDED.mrp_paise,
          in_stock = EXCLUDED.in_stock,
          image_url = COALESCE(EXCLUDED.image_url, listing.image_url),
          raw_attributes = EXCLUDED.raw_attributes,
          match_status = EXCLUDED.match_status,
          match_method = EXCLUDED.match_method,
          match_key_candidate = EXCLUDED.match_key_candidate,
          match_notes = EXCLUDED.match_notes,
          last_seen_at = now()
        RETURNING
          id,
          affiliate_url,
          affiliated,
          affiliate_provider::text,
          affiliate_checked_at
    """

    upserted_rows: List[UpsertedListingRow] = []
    for chunk in _chunked(items, chunk_size):
        params_seq = []
        for it in chunk:
            ext = it.outcome.extraction
            raw_attrs_payload = {
                "cleaned_title": ext.cleaned_title,
                "brand_slug": ext.brand_slug,
                "canonical_mpn": ext.mpn,
                "attributes": ext.attributes,
            }
            params_seq.append(
                (
                    it.retailer_id,
                    it.product_id,
                    it.category_id,
                    it.listing.url,
                    it.listing.external_id,
                    it.listing.raw_title,
                    it.listing.raw_brand,
                    it.listing.raw_mpn,
                    it.listing.price_paise,
                    it.listing.mrp_paise,
                    it.listing.in_stock,
                    it.listing.image_url,
                    json.dumps(raw_attrs_payload),
                    it.outcome.match_status,
                    it.outcome.match_method,
                    it.outcome.match_key_candidate,
                    it.outcome.match_notes,
                )
            )
        with conn.cursor() as cur:
            cur.executemany(sql, params_seq, returning=True)
            while True:
                row = cur.fetchone()
                if row is not None:
                    upserted_rows.append(
                        UpsertedListingRow(
                            listing_id=int(row[0]),
                            affiliate_url=str(row[1]) if row[1] else None,
                            affiliated=bool(row[2]),
                            affiliate_provider=str(row[3]) if row[3] else None,
                            affiliate_checked_at=row[4],
                        )
                    )
                if not cur.nextset():
                    break
        conn.commit()

    return upserted_rows


def upsert_listing(
    conn: psycopg.Connection[Any],
    listing: ScrapedListing,
    outcome: MatchOutcome,
    *,
    retailer_id: int,
    category_id: int,
    product_id: Optional[int],
) -> UpsertedListingRow:
    """Upsert a single `listing` row on `(retailer_id, url)` and update `last_seen_at = now()`."""
    rows = bulk_upsert_listings(
        conn,
        [
            ListingUpsertItem(
                listing=listing,
                outcome=outcome,
                retailer_id=retailer_id,
                category_id=category_id,
                product_id=product_id,
            )
        ],
        chunk_size=1,
    )
    if not rows:
        raise RuntimeError(f"Failed to upsert listing for url={listing.url}")
    return rows[0]


def update_listing_affiliate(
    conn: psycopg.Connection[Any],
    listing_id: int,
    resolution: AffiliateResolution,
) -> None:
    """Update a listing's `affiliate_url`, `affiliated`, `affiliate_provider`, and `affiliate_checked_at`."""
    batch_update_listing_affiliates(conn, [(listing_id, resolution)])
    conn.commit()


def batch_update_listing_affiliates(
    conn: psycopg.Connection[Any],
    updates: Sequence[Tuple[int, AffiliateResolution]],
    *,
    chunk_size: int = BULK_CHUNK_SIZE,
) -> None:
    """Batch-update affiliate fields for multiple listings in chunked `executemany` blocks."""
    if not updates:
        return
    sql = """
        UPDATE listing
        SET affiliate_url = %s,
            affiliated = %s,
            affiliate_provider = %s::"AffiliateProvider",
            affiliate_checked_at = %s
        WHERE id = %s
    """
    for chunk in _chunked(updates, chunk_size):
        with conn.cursor() as cur:
            cur.executemany(
                sql,
                [
                    (
                        res.affiliate_url,
                        res.affiliated,
                        res.affiliate_provider,
                        res.checked_at,
                        lid,
                    )
                    for lid, res in chunk
                ],
            )
        conn.commit()


# Amazon prices may only be shown while they are under 24 hours old (Amazon's rule). An Amazon
# listing that hasn't been refreshed for 24 hours becomes "link-only": the website shows it as a
# plain store link with no price, and it must not set the cached best price, store count or price
# history. Needs `l` (listing) and `r` (retailer) aliases. Mirrors web/src/lib/linkOnly.ts.
LINK_ONLY_LISTING_SQL = """(
  r.slug = %(link_only_retailer)s
  AND l.last_seen_at <= now() - make_interval(hours => %(link_only_max_age_hours)s::int)
)"""


def _link_only_params() -> dict[str, Any]:
    return {
        "link_only_retailer": "amazon",
        "link_only_max_age_hours": AMAZON_PRICE_MAX_AGE_HOURS,
    }


class ConnectionKeepAlive:
    """Runs `SELECT 1` on a connection every `interval` seconds from a background thread.

    Long fetch phases (the Amazon sync searches for several minutes before writing anything) leave
    the connection unused, and Neon drops connections that sit idle for about 5 minutes, so the
    first write then fails with "SSL connection has been closed". psycopg 3 connections are
    thread-safe, and the main thread doesn't use the connection while this is running.
    """

    def __init__(self, conn: psycopg.Connection[Any], interval: float = 60.0) -> None:
        self.conn = conn
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pings = 0

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                idle = self.conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
                self.conn.execute("SELECT 1")
                if idle:
                    self.conn.rollback()  # leave no transaction open that we started
                self.pings += 1
            except Exception as exc:  # never break the run because of a ping
                logger.warning("[db] keep-alive ping failed: %s", exc)
                return

    def start(self) -> "ConnectionKeepAlive":
        self._thread = threading.Thread(target=self._run, name="db-keepalive", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)


def load_known_amazon_asins(
    conn: psycopg.Connection[Any],
    retailer_id: int,
    categories: Sequence[str],
) -> dict[str, list[str]]:
    """ASINs of matched Amazon listings per category, least recently checked first.

    The Amazon sync re-checks these by ID (10 per request), which is much cheaper than searching,
    and keeps their prices under Amazon's 24-hour display limit.
    """
    out: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.slug, l.external_id
            FROM listing l
            JOIN category c ON c.id = l.category_id
            WHERE l.retailer_id = %(retailer_id)s
              AND l.external_id IS NOT NULL
              AND l.match_status = 'MATCHED'
              AND c.slug = ANY(%(categories)s)
            ORDER BY l.last_seen_at ASC
            """,
            {"retailer_id": retailer_id, "categories": list(categories)},
        )
        for slug, asin in cur.fetchall():
            asin = str(asin).strip().upper()
            if asin and asin not in out.setdefault(slug, []):
                out[slug].append(asin)
    return out


def refresh_product_rollups(conn: psycopg.Connection[Any]) -> int:
    """Refresh `product.best_price_paise` and `product.store_count` across active retailers.

    Deduplicates multiple listings from the same retailer (`DISTINCT ON (l.product_id, l.retailer_id)`).
    Prefers fresh offers (`last_seen_at <= max_age_hours`), falling back to the last checked offers
    from active retailers when scrapers are paused so the catalog never goes blank.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE product p
            SET best_price_paise = NULL,
                store_count = 0
            WHERE NOT EXISTS (
              SELECT 1
              FROM listing l
              JOIN retailer r ON r.id = l.retailer_id
              WHERE l.product_id = p.id
                AND l.match_status = 'MATCHED'
                AND r.is_active = TRUE
                AND NOT {LINK_ONLY_SQL}
            )
            AND (p.best_price_paise IS NOT NULL OR p.store_count <> 0)
            """.replace("{LINK_ONLY_SQL}", LINK_ONLY_LISTING_SQL),
            _link_only_params(),
        )

        cur.execute(
            """
            WITH deduped_offers AS (
              SELECT DISTINCT ON (l.product_id, l.retailer_id)
                l.product_id,
                l.retailer_id,
                l.price_paise,
                l.in_stock,
                (l.last_seen_at > now() - make_interval(hours => r.max_age_hours)) AS is_fresh
              FROM listing l
              JOIN retailer r ON r.id = l.retailer_id
              WHERE l.match_status = 'MATCHED'
                AND r.is_active = TRUE
                AND NOT {LINK_ONLY_SQL}
              ORDER BY
                l.product_id,
                l.retailer_id,
                (l.last_seen_at > now() - make_interval(hours => r.max_age_hours)) DESC,
                l.in_stock DESC,
                l.last_seen_at DESC,
                l.price_paise ASC
            ),
            rollup AS (
              SELECT
                d.product_id,
                COALESCE(
                  MIN(d.price_paise) FILTER (WHERE d.in_stock AND d.is_fresh),
                  MIN(d.price_paise) FILTER (WHERE d.in_stock)
                ) AS best,
                COALESCE(
                  NULLIF(COUNT(*) FILTER (WHERE d.is_fresh), 0),
                  COUNT(*)
                ) AS stores
              FROM deduped_offers d
              GROUP BY d.product_id
            )
            UPDATE product p SET
              best_price_paise = s.best,
              store_count      = s.stores,
              updated_at       = now()
            FROM rollup s
            WHERE p.id = s.product_id
            """.replace("{LINK_ONLY_SQL}", LINK_ONLY_LISTING_SQL),
            _link_only_params(),
        )
        updated_count = cur.rowcount or 0
    conn.commit()
    return updated_count


def record_price_snapshots(conn: psycopg.Connection[Any]) -> int:
    """Store today's (IST) lowest in-stock price and store count for every listed product.

    Feeds the price-history charts and the price alerts on the website. Runs after each
    `refresh_product_rollups`; several runs on one day keep the day's lowest price. Skipped
    quietly until the website migration that creates `price_snapshot` has been applied.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('price_snapshot') IS NOT NULL")
        row = cur.fetchone()
        if not (row and row[0]):
            return 0
        cur.execute(
            """
            INSERT INTO price_snapshot (product_id, day, min_price_paise, store_count, updated_at)
            SELECT p.id, (now() AT TIME ZONE 'Asia/Kolkata')::date, p.best_price_paise, p.store_count, now()
            FROM product p
            WHERE p.store_count > 0
            ON CONFLICT (product_id, day) DO UPDATE SET
              min_price_paise = CASE
                WHEN price_snapshot.min_price_paise IS NULL THEN EXCLUDED.min_price_paise
                WHEN EXCLUDED.min_price_paise IS NULL THEN price_snapshot.min_price_paise
                ELSE LEAST(price_snapshot.min_price_paise, EXCLUDED.min_price_paise)
              END,
              store_count = GREATEST(price_snapshot.store_count, EXCLUDED.store_count),
              updated_at = now()
            """
        )
        written = cur.rowcount or 0
    conn.commit()
    return written


def fetch_database_report(conn: psycopg.Connection[Any]) -> DatabaseReport:
    """Fetch last `scrape_run` per retailer, listing match/affiliate stats, and product rollups."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              r.slug AS retailer_slug,
              r.name AS retailer_name,
              sr.id AS run_id,
              sr.status::text AS status,
              COALESCE(sr.items_found, 0) AS items_found,
              COALESCE(sr.items_ok, 0) AS items_ok,
              COALESCE(sr.items_failed, 0) AS items_failed,
              sr.error,
              sr.started_at,
              sr.finished_at
            FROM retailer r
            LEFT JOIN LATERAL (
              SELECT *
              FROM scrape_run s
              WHERE s.retailer_id = r.id
              ORDER BY s.started_at DESC, s.id DESC
              LIMIT 1
            ) sr ON TRUE
            ORDER BY r.id ASC
            """
        )
        run_rows = [
            RetailerRunSummaryRow(
                retailer_slug=str(row["retailer_slug"]),
                retailer_name=str(row["retailer_name"]),
                run_id=int(row["run_id"]) if row["run_id"] is not None else None,
                status=str(row["status"]) if row["status"] is not None else None,
                items_found=int(row["items_found"]),
                items_ok=int(row["items_ok"]),
                items_failed=int(row["items_failed"]),
                error=str(row["error"]) if row["error"] else None,
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT
              r.slug AS retailer_slug,
              r.name AS retailer_name,
              COUNT(l.id) AS total_listings,
              COUNT(l.id) FILTER (WHERE l.match_status = 'MATCHED') AS matched_count,
              COUNT(l.id) FILTER (WHERE l.match_status = 'REVIEW') AS review_count,
              COUNT(l.id) FILTER (WHERE l.match_status = 'UNMATCHED') AS unmatched_count,
              COUNT(l.id) FILTER (WHERE l.affiliated = TRUE AND l.affiliate_url IS NOT NULL) AS affiliated_count,
              COUNT(l.id) FILTER (WHERE l.affiliated = FALSE OR l.affiliate_url IS NULL) AS plain_count
            FROM retailer r
            LEFT JOIN listing l ON l.retailer_id = r.id
            GROUP BY r.id, r.slug, r.name
            ORDER BY r.id ASC
            """
        )
        stat_rows = [
            RetailerListingStatsRow(
                retailer_slug=str(row["retailer_slug"]),
                retailer_name=str(row["retailer_name"]),
                total_listings=int(row["total_listings"]),
                matched_count=int(row["matched_count"]),
                review_count=int(row["review_count"]),
                unmatched_count=int(row["unmatched_count"]),
                affiliated_count=int(row["affiliated_count"]),
                plain_count=int(row["plain_count"]),
            )
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT
              COUNT(*) AS total_products,
              COUNT(*) FILTER (WHERE store_count >= 2) AS multi_store_products,
              COUNT(*) FILTER (WHERE best_price_paise IS NOT NULL) AS priced_products
            FROM product
            """
        )
        prod_row = cur.fetchone() or {
            "total_products": 0,
            "multi_store_products": 0,
            "priced_products": 0,
        }

    return DatabaseReport(
        last_runs=run_rows,
        retailer_stats=stat_rows,
        total_products=int(prod_row["total_products"]),
        multi_store_products=int(prod_row["multi_store_products"]),
        priced_products=int(prod_row["priced_products"]),
    )
