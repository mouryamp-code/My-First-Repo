"""Targeted lookup: search specific stores for products we already know, so more products can be
compared across stores. Runs once a day.

How it works
------------
1. **Pick targets** (`targeted_db.select_targets`): in-stock products listed by 1–2 stores that a
   given store doesn't list yet, most valuable first, skipping lookups that aren't due
   (misses back off 3 → 7 → 14 → 30 days; found products are refreshed daily).
2. **Build short, distinctive queries** ("9800X3D", "Zotac 5060 Ti", "Corsair RM850e"). Products
   that share a query are searched once.
3. **Search** each store through its own search endpoint, in parallel across stores (each store
   keeps its own polite rate limit, robots.txt check and backoff).
4. **Accept strictly**: a search result is kept only if it normalizes to the match key (or MPN) of
   a product that already exists. Search noise can never create new products.
5. **Ingest** accepted listings through the normal pipeline (matcher, upsert, affiliate links,
   price rollups, scrape_run record), then record every lookup outcome.

Stores covered: every website store is now crawled in full (all pages of every category), so the
daily job only searches **Amazon**, whose catalog can only be reached through the Creators API.
Vedant and MD Computers stay searchable on demand (`--retailer vedant`) for one-off checks.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pcforge_scraper.config import CategorySlug, RetailerSlug, Settings, get_settings, load_targets
from pcforge_scraper.normalize.attributes import extract_attributes
from pcforge_scraper.normalize.keygen import build_match_key
from pcforge_scraper.normalize.matcher import MatchedProduct
from pcforge_scraper.retailers.base import RetailerSource, ScrapedListing
from pcforge_scraper.targeted_db import LookupResult, LookupTarget

logger = logging.getLogger(__name__)

#: Default number of products looked up per store per run.
DEFAULT_BUDGETS: dict[str, int] = {"vedant": 120, "mdcomputers": 120, "amazon": 200}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _words(slug: str | None) -> list[str]:
    return [w for w in re.split(r"[-\s]+", str(slug or "").lower()) if w]


def _is_code(token: str) -> bool:
    """A distinctive part code: letters and digits, at least 4 chars ("rm850e", "27gr75q", "sn770")."""
    return len(token) >= 4 and bool(re.search(r"[a-z]", token)) and bool(re.search(r"\d", token))


def _pretty(tokens: list[str]) -> str:
    out = []
    for t in tokens:
        out.append(t.upper() if re.search(r"\d", t) or len(t) <= 3 else t.capitalize())
    return " ".join(out)


#: How stores actually write some brands in titles.
_BRAND_IN_TITLES = {"western digital": "WD", "be quiet!": "be quiet", "g.skill": "G.Skill", "teamgroup": "Team"}
#: Placeholder or spec-like "model" words that would only pollute a query.
_JUNK_WORDS = {"standard", "generic", "processor", "monitor", "fan", "cabinet", "psu", "gpu"}


def _brand_token(brand: str) -> str:
    return _BRAND_IN_TITLES.get(brand.lower(), brand)


def _model_words(value: Any) -> list[str]:
    return [w for w in _words(value) if w not in _JUNK_WORDS and not re.fullmatch(r"\d+(?:\.\d+)?x\d+|\d+\.\d+", w)]


def build_query(target: LookupTarget) -> str:
    """A short search string that every store's title for this product should contain.

    Kept broad on purpose (e.g. no GPU series): the strict key match after the search picks the
    exact product, and a broad query lets one search serve several products.
    """
    a = target.attributes
    cat = target.category
    brand = _brand_token(target.brand) if target.brand else ""
    model = _model_words(a.get("model"))

    if cat == "cpu":
        code = next((t for t in reversed(model) if re.search(r"\d", t)), None)
        if code:
            return code.upper()
    elif cat == "gpu":
        chip = _words(a.get("chipset"))  # ["rtx", "5060", "ti"]
        core = [t for t in chip if t not in ("rtx", "gtx", "rx", "gt", "arc")]
        if core:
            return f"{brand} {_pretty(core)}".strip()
    elif cat == "ram":
        series = _model_words(a.get("series"))[:2]
        cap = str(a.get("total_capacity") or "").upper()
        mem = str(a.get("ram_type") or "").upper()
        return " ".join(x for x in (brand, _pretty(series), cap, "" if series else mem) if x)
    elif cat == "hdd":
        # Series already carries the brand family ("wd-purple", "barracuda", "ironwolf").
        series = _model_words(a.get("series"))[:2]
        cap = str(a.get("capacity") or "").upper()
        lead = "" if series and series[0] in ("wd",) else brand
        return " ".join(x for x in (lead, _pretty(series), cap) if x)
    elif cat == "motherboard":
        if model:
            return f"{brand} {_pretty(model[:2])}".strip()

    # Everything else: a distinctive code alone, or brand + the first model words.
    code = next((t for t in model if _is_code(t)), None)
    if code and cat in ("monitor", "ssd", "psu"):
        return code.upper()
    if model:
        return f"{brand} {_pretty(model[:2])}".strip()
    return " ".join(target.name.split()[:4])


def amazon_query(target: LookupTarget) -> str:
    """Amazon ranks by relevance, so the full canonical name works best there."""
    return re.sub(r"\s*\((?:tray|box|black)\)", "", target.name, flags=re.I)[:120]


# ---------------------------------------------------------------------------
# Searchers
# ---------------------------------------------------------------------------

class Searcher(Protocol):
    retailer: RetailerSlug
    categories: tuple[str, ...]

    def query_for(self, target: LookupTarget) -> str: ...
    def search(self, query: str, category: CategorySlug) -> list[ScrapedListing]: ...
    def close(self) -> None: ...


class OpenCartSearcher:
    """Vedant / MD Computers: OpenCart's search page uses the same product grid as category pages,
    so the store's existing category parser reads it."""

    def __init__(self, scraper: Any) -> None:
        self.scraper = scraper
        self.retailer: RetailerSlug = scraper.slug
        self.categories = tuple(scraper.supported_categories)

    def query_for(self, target: LookupTarget) -> str:
        return build_query(target)

    def search(self, query: str, category: CategorySlug) -> list[ScrapedListing]:
        url = f"{self.scraper.base_url}/index.php"
        resp = self.scraper.fetcher.get(url, params={"route": "product/search", "search": query})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return list(self.scraper.parse_category_html(resp.text, category))

    def close(self) -> None:
        close = getattr(self.scraper, "close", None)
        if callable(close):
            close()


class AmazonSearcher:
    """Amazon Creators API keyword search (10 results per call)."""

    def __init__(self, scraper: Any) -> None:
        self.scraper = scraper
        self.retailer: RetailerSlug = "amazon"
        self.categories = tuple(scraper.supported_categories)

    def query_for(self, target: LookupTarget) -> str:
        return amazon_query(target)

    def search(self, query: str, category: CategorySlug) -> list[ScrapedListing]:
        listings, _ = self.scraper.search_items(query, category, item_count=10)
        return list(listings)

    def close(self) -> None:
        close = getattr(self.scraper, "close", None)
        if callable(close):
            close()


def make_searcher(retailer: str) -> Searcher | None:
    from pcforge_scraper.retailers.amazon import AmazonScraper
    from pcforge_scraper.retailers.mdcomputers import MDComputersScraper
    from pcforge_scraper.retailers.vedant import VedantScraper

    targets = load_targets()
    if retailer == "vedant":
        return OpenCartSearcher(VedantScraper(targets=targets))
    if retailer == "mdcomputers":
        return OpenCartSearcher(MDComputersScraper(targets=targets))
    if retailer == "amazon":
        scraper = AmazonScraper(targets=targets)
        if not scraper.has_credentials():
            scraper.close()
            logger.warning("[targeted] Amazon credentials missing: skipping Amazon")
            return None
        return AmazonSearcher(scraper)
    return None


SEARCHABLE_RETAILERS: tuple[str, ...] = ("vedant", "mdcomputers", "amazon")
#: Searched by the daily job. The website stores are full-catalog crawls, so searching them
#: would only re-find what the main scraper already lists.
DEFAULT_RETAILERS: tuple[str, ...] = ("amazon",)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

@dataclass
class KnownProducts:
    """Existing products indexed by match key and (category, MPN)."""

    by_key: dict[str, MatchedProduct]
    by_mpn: dict[tuple[str, str], MatchedProduct]

    @classmethod
    def from_products(cls, products: list[MatchedProduct]) -> KnownProducts:
        by_mpn: dict[tuple[str, str], MatchedProduct] = {(p.category, p.mpn.upper()): p for p in products if p.mpn}
        return cls(by_key={p.match_key: p for p in products}, by_mpn=by_mpn)

    def identify(self, listing: ScrapedListing) -> MatchedProduct | None:
        """The existing product this listing is, or None (never invents a product)."""
        ex = extract_attributes(
            listing.category,
            listing.raw_title,
            raw_brand=listing.raw_brand,
            raw_mpn=listing.raw_mpn,
            retailer=listing.retailer,
            source_fields=listing.source_fields,
        )
        if ex.conflict_notes:
            return None
        if ex.mpn and (listing.category, ex.mpn.upper()) in self.by_mpn:
            return self.by_mpn[(listing.category, ex.mpn.upper())]
        key = build_match_key(ex)
        return self.by_key.get(key) if key else None


# ---------------------------------------------------------------------------
# Running one store
# ---------------------------------------------------------------------------

@dataclass
class StoreRun:
    retailer: str
    targets: int = 0
    queries: int = 0
    requests_failed: int = 0
    found: int = 0
    #: Other known products that turned up in results and are new at this store.
    bonus: int = 0
    refreshed: int = 0
    seconds: float = 0.0
    due_total: int = 0
    accepted: list[ScrapedListing] = field(default_factory=list)
    results: list[LookupResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def search_store(
    searcher: Searcher,
    targets: list[LookupTarget],
    known: KnownProducts,
    *,
    max_requests: int,
    already_here: set[int] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> StoreRun:
    """Run every due lookup for one store. No database access: safe to run in a thread."""
    run = StoreRun(retailer=searcher.retailer, targets=len(targets))
    started = time.monotonic()

    # One request per distinct (category, query); products sharing a query reuse its results.
    groups: dict[tuple[str, str], list[LookupTarget]] = defaultdict(list)
    for t in targets:
        groups[(t.category, searcher.query_for(t))].append(t)

    seen_urls: set[str] = set()
    found_ids: set[int] = set()
    failed_ids: set[int] = set()
    consecutive_failures = 0
    for (category, query), group in groups.items():
        if run.queries >= max_requests:
            break
        if consecutive_failures >= 5:
            run.errors.append("stopped after 5 failed searches in a row")
            break
        run.queries += 1
        try:
            results = searcher.search(query, category)  # type: ignore[arg-type]
            consecutive_failures = 0
        except Exception as exc:  # one failed search shouldn't end the run
            run.requests_failed += 1
            consecutive_failures += 1
            failed_ids.update(t.product_id for t in group)
            run.errors.append(f"{query!r}: {exc}")
            logger.warning("[targeted:%s] search %r failed: %s", searcher.retailer, query, exc)
            continue

        for listing in results:
            product = known.identify(listing)
            if product is None or listing.url in seen_urls:
                continue
            seen_urls.add(listing.url)
            run.accepted.append(listing)
            found_ids.add(product.id)

        if on_progress:
            on_progress(f"[{searcher.retailer}] {run.queries}/{len(groups)} {query!r}: {len(results)} results")

    target_ids = {t.product_id for t in targets}
    searched = {t.product_id for _key, g in list(groups.items())[: run.queries] for t in g}
    for (_category, query), group in groups.items():
        for t in group:
            if t.product_id not in searched or t.product_id in failed_ids:
                continue  # not searched this run (budget) or the search errored: try again next run
            hit = t.product_id in found_ids
            run.results.append(LookupResult(product_id=t.product_id, query=query, found=hit))
            if hit:
                run.found += 1
                run.refreshed += int(t.refresh)
    run.bonus = len(found_ids - target_ids - (already_here or set()))
    run.seconds = time.monotonic() - started
    return run


class PrefetchedSource(RetailerSource):
    """Feeds already-gathered listings into `ingest_retailer`."""

    def __init__(self, retailer: RetailerSlug, listings: list[ScrapedListing]) -> None:
        super().__init__(targets=load_targets())
        self.slug = retailer
        self._by_cat: dict[str, list[ScrapedListing]] = defaultdict(list)
        for item in listings:
            self._by_cat[item.category].append(item)
        self.supported_categories = tuple(self._by_cat)  # type: ignore[assignment,arg-type]

    @property
    def categories(self) -> list[CategorySlug]:
        return list(self._by_cat)  # type: ignore[arg-type]

    def fetch_category(self, category, quota=None, seeds=None):  # type: ignore[no-untyped-def]
        return list(self._by_cat.get(category, []))


# ---------------------------------------------------------------------------
# Whole job
# ---------------------------------------------------------------------------

@dataclass
class TargetedReport:
    stores: list[StoreRun]
    dry_run: bool
    revalidated: bool = False


def run_targeted_lookup(
    *,
    retailers: list[str] | None = None,
    budgets: dict[str, int] | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> TargetedReport:
    """Select targets, search stores in parallel, ingest what's found and record every lookup."""
    from pcforge_scraper.affiliate.cuelinks import CuelinksClient
    from pcforge_scraper.db import connect_db, ensure_reference_data, load_existing_products
    from pcforge_scraper.ingest import ingest_retailer, trigger_web_revalidation
    from pcforge_scraper.normalize.matcher import ProductMatcher
    from pcforge_scraper.targeted_db import (
        ensure_lookup_table,
        lookup_table_exists,
        products_listed_at,
        record_results,
        retailer_ids,
        select_targets,
    )

    cfg = settings or get_settings()
    wanted = [r for r in (retailers or list(DEFAULT_RETAILERS)) if r in SEARCHABLE_RETAILERS]
    budget_for = {**DEFAULT_BUDGETS, **(budgets or {})}

    searchers: dict[str, Searcher] = {}
    for r in wanted:
        s = make_searcher(r)
        if s is not None:
            searchers[r] = s
    if not searchers:
        return TargetedReport(stores=[], dry_run=dry_run)

    try:
        # Phase 1 (DB): pick targets. The connection is closed before the slow network phase.
        with connect_db(cfg) as conn:
            if dry_run:  # strictly read-only
                rids = retailer_ids(conn)
                has_history = lookup_table_exists(conn)
            else:
                rids = ensure_reference_data(conn).retailers
                ensure_lookup_table(conn)
                has_history = True
            existing = load_existing_products(conn)
            listed = {r: products_listed_at(conn, rids[r]) for r in searchers}
            selections = {
                r: select_targets(
                    conn,
                    retailer_id=rids[r],
                    categories=s.categories,
                    limit=budget_for.get(r, 50),
                    has_history=has_history,
                )
                for r, s in searchers.items()
            }
        known = KnownProducts.from_products(existing)

        # Phase 2 (network): one thread per store; each store keeps its own rate limiter.
        with ThreadPoolExecutor(max_workers=len(searchers)) as pool:
            futures = {
                r: pool.submit(
                    search_store,
                    searchers[r],
                    selections[r].targets,
                    known,
                    max_requests=budget_for.get(r, 50),
                    already_here=listed[r],
                    on_progress=on_progress,
                )
                for r in searchers
            }
            runs = {r: f.result() for r, f in futures.items()}
        for r, run in runs.items():
            run.due_total = selections[r].due_total
    finally:
        for s in searchers.values():
            s.close()

    report = TargetedReport(stores=list(runs.values()), dry_run=dry_run)
    if dry_run:
        return report

    # Phase 3 (DB): ingest through the normal pipeline, then record lookup outcomes.
    with connect_db(cfg) as conn:
        refs = ensure_reference_data(conn)
        matcher = ProductMatcher(existing_products=load_existing_products(conn))
        with CuelinksClient(settings=cfg) as cuelinks:
            for r, run in runs.items():
                if run.accepted:
                    source = PrefetchedSource(r, run.accepted)  # type: ignore[arg-type]
                    outcome = ingest_retailer(
                        conn,
                        retailer=r,  # type: ignore[arg-type]
                        categories=source.categories,
                        targets=load_targets(),
                        matcher=matcher,
                        cuelinks_client=cuelinks,
                        retailer_id=refs.retailers[r],
                        category_ids=refs.categories,
                        brand_ids=refs.brands,
                        settings=cfg,
                        source=source,
                    )
                    if outcome.error:
                        run.errors.append(f"ingest: {outcome.error}")
                record_results(conn, retailer_id=refs.retailers[r], results=run.results)
    report.revalidated = trigger_web_revalidation(cfg)
    return report
