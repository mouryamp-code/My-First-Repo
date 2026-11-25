"""Database access for the targeted lookup job (see `targeted.py`).

`targeted_lookup` remembers every (product, retailer) search so a store that doesn't sell a product
isn't asked about it every day:

- not found  -> retried after 3, 7, 14, then every 30 days
- found      -> re-checked daily, so the store's price for it stays fresh
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import psycopg

LOOKUP_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS targeted_lookup (
    product_id   INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    retailer_id  INTEGER NOT NULL REFERENCES retailer(id) ON DELETE CASCADE,
    attempts     INTEGER NOT NULL DEFAULT 0,
    found        BOOLEAN NOT NULL DEFAULT FALSE,
    last_query   TEXT,
    last_tried_at TIMESTAMPTZ,
    next_due_at  TIMESTAMPTZ,
    PRIMARY KEY (product_id, retailer_id)
);
CREATE INDEX IF NOT EXISTS targeted_lookup_due_idx ON targeted_lookup (retailer_id, next_due_at);
"""

#: Retry delay after the Nth consecutive miss.
MISS_BACKOFF_DAYS = (3, 7, 14, 30)
#: A product found at a store is searched again after this many hours to refresh its price.
FOUND_REFRESH_HOURS = 20

#: Categories people compare most come first when the budget is tight.
CATEGORY_PRIORITY = (
    "gpu", "cpu", "motherboard", "ram", "ssd", "psu", "monitor", "cooler",
    "case", "keyboard", "mouse", "headset", "hdd", "fan", "controller",
)


@dataclass
class LookupTarget:
    product_id: int
    name: str
    slug: str
    category: str
    brand: str
    match_key: str
    mpn: str | None
    attributes: dict[str, Any]
    store_count: int
    price_paise: int | None
    refresh: bool
    attempts: int


@dataclass
class LookupResult:
    product_id: int
    query: str
    found: bool


@dataclass
class TargetSelection:
    targets: list[LookupTarget] = field(default_factory=list)
    due_total: int = 0


def lookup_table_exists(conn: psycopg.Connection[Any]) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('targeted_lookup') IS NOT NULL")
        row = cur.fetchone()
    return bool(row and row[0])


def retailer_ids(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Read-only retailer slug -> id map (for dry runs that must not write)."""
    with conn.cursor() as cur:
        cur.execute("SELECT slug, id FROM retailer")
        return {str(slug): int(rid) for slug, rid in cur.fetchall()}


def products_listed_at(conn: psycopg.Connection[Any], retailer_id: int) -> set[int]:
    """Products this retailer already has a matched listing for."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT product_id FROM listing WHERE retailer_id = %s AND match_status = 'MATCHED' AND product_id IS NOT NULL",
            (retailer_id,),
        )
        return {int(r[0]) for r in cur.fetchall()}


def ensure_lookup_table(conn: psycopg.Connection[Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(LOOKUP_TABLE_DDL)
    conn.commit()


def select_targets(
    conn: psycopg.Connection[Any],
    *,
    retailer_id: int,
    categories: Sequence[str],
    limit: int,
    max_store_count: int = 2,
    has_history: bool = True,
) -> TargetSelection:
    """Products worth searching for at `retailer_id`, most valuable first.

    A product qualifies when it is in stock somewhere (seen in the last 3 days), is listed by at
    most `max_store_count` stores, isn't already fresh at this retailer, and its lookup is due.
    Products this retailer was found to sell come first (price refresh), then single-store
    products, then by category priority and price.
    """
    priority_sql = "CASE c.slug " + " ".join(f"WHEN '{slug}' THEN {i}" for i, slug in enumerate(CATEGORY_PRIORITY)) + " ELSE 99 END"
    sql = f"""
        WITH offers AS (
            SELECT l.product_id,
                   COUNT(DISTINCT l.retailer_id) AS store_count,
                   BOOL_OR(l.in_stock AND l.last_seen_at > now() - interval '72 hours') AS live,
                   MIN(l.price_paise) FILTER (WHERE l.in_stock) AS price_paise,
                   BOOL_OR(l.retailer_id = %(rid)s AND l.last_seen_at > now() - interval '{FOUND_REFRESH_HOURS} hours') AS fresh_here
            FROM listing l
            JOIN retailer r ON r.id = l.retailer_id
            WHERE l.match_status = 'MATCHED' AND l.product_id IS NOT NULL AND r.is_active
            GROUP BY l.product_id
        ),
        due AS (
            SELECT p.id, p.name, p.slug, p.match_key, p.mpn, p.attributes,
                   c.slug AS category, b.name AS brand,
                   o.store_count, o.price_paise,
                   COALESCE(tl.found, FALSE) AS refresh,
                   COALESCE(tl.attempts, 0) AS attempts,
                   {priority_sql} AS cat_priority
            FROM product p
            JOIN offers o ON o.product_id = p.id
            JOIN category c ON c.id = p.category_id
            JOIN brand b ON b.id = p.brand_id
            LEFT JOIN {"targeted_lookup" if has_history else "(SELECT NULL::int AS product_id, NULL::int AS retailer_id, NULL::bool AS found, NULL::int AS attempts, NULL::timestamptz AS next_due_at)"} tl
                ON tl.product_id = p.id AND tl.retailer_id = %(rid)s
            WHERE o.live
              AND NOT o.fresh_here
              AND c.slug = ANY(%(cats)s)
              AND (o.store_count <= %(max_stores)s OR COALESCE(tl.found, FALSE))
              AND (tl.next_due_at IS NULL OR tl.next_due_at <= now())
        )
        SELECT *, COUNT(*) OVER () AS due_total,
               -- Round-robin across categories, in a different (deterministic) order each day,
               -- so every run samples the whole catalog instead of one category's extremes.
               ROW_NUMBER() OVER (
                   PARTITION BY refresh, category
                   ORDER BY store_count ASC, md5(id::text || current_date::text)
               ) AS turn
        FROM due
        ORDER BY refresh DESC, turn ASC, cat_priority ASC
        LIMIT %(limit)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"rid": retailer_id, "cats": list(categories), "max_stores": max_store_count, "limit": limit})
        cols = [d.name for d in cur.description or []]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    selection = TargetSelection(due_total=int(rows[0]["due_total"]) if rows else 0)
    for r in rows:
        selection.targets.append(
            LookupTarget(
                product_id=int(r["id"]),
                name=str(r["name"]),
                slug=str(r["slug"]),
                category=str(r["category"]),
                brand=str(r["brand"]),
                match_key=str(r["match_key"]),
                mpn=r["mpn"],
                attributes=r["attributes"] if isinstance(r["attributes"], dict) else {},
                store_count=int(r["store_count"]),
                price_paise=r["price_paise"],
                refresh=bool(r["refresh"]),
                attempts=int(r["attempts"]),
            )
        )
    return selection


def record_results(conn: psycopg.Connection[Any], *, retailer_id: int, results: Sequence[LookupResult]) -> None:
    """Upsert lookup outcomes and schedule each product's next search at this retailer."""
    if not results:
        return
    backoff = "ARRAY[" + ",".join(str(d) for d in MISS_BACKOFF_DAYS) + "]"
    sql = f"""
        INSERT INTO targeted_lookup AS t (product_id, retailer_id, attempts, found, last_query, last_tried_at, next_due_at)
        VALUES (%(pid)s, %(rid)s, CASE WHEN %(found)s THEN 0 ELSE 1 END, %(found)s, %(query)s, now(),
                CASE WHEN %(found)s THEN now() + interval '{FOUND_REFRESH_HOURS} hours'
                     ELSE now() + make_interval(days => ({backoff})[1]) END)
        ON CONFLICT (product_id, retailer_id) DO UPDATE SET
            found = EXCLUDED.found,
            last_query = EXCLUDED.last_query,
            last_tried_at = now(),
            attempts = CASE WHEN EXCLUDED.found THEN 0 ELSE t.attempts + 1 END,
            next_due_at = CASE
                WHEN EXCLUDED.found THEN now() + interval '{FOUND_REFRESH_HOURS} hours'
                ELSE now() + make_interval(days => ({backoff})[LEAST(t.attempts + 1, {len(MISS_BACKOFF_DAYS)})])
            END
    """
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [{"pid": r.product_id, "rid": retailer_id, "found": r.found, "query": r.query[:200]} for r in results],
        )
    conn.commit()
