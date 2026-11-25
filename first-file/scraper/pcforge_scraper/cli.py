"""Typer CLI for scraper (`pcforge-scrape` and `pcforge-normalize`)."""

import logging
import random
import time
from pathlib import Path
from typing import Annotated, List, Optional, cast

import typer
from rich.console import Console
from rich.table import Table

from pcforge_scraper.config import (
    ALL_CATEGORIES,
    CATEGORY_GROUPS,
    CORE_CATEGORIES,
    SCRAPER_DIR,
    CategorySlug,
    RetailerSlug,
    load_targets,
)
from pcforge_scraper.db import DatabaseReport, connect_db, fetch_database_report
from pcforge_scraper.ingest import run_ingestion_pipeline
from pcforge_scraper.normalize import (
    ProductMatcher,
    build_canonical_name_and_slug,
    build_match_key,
    extract_attributes,
    infer_category_from_title,
    parse_price_paise,
)
from pcforge_scraper.retailers import SCRAPER_CLASSES, ScrapedListing, get_retailer_source

logger = logging.getLogger(__name__)

RETAILER_ALIASES: dict[str, RetailerSlug] = {
    "computechstore": "computech",
    "computech-store": "computech",
    "flipcart": "flipkart",
    "md": "mdcomputers",
    "vedantcomputers": "vedant",
    "prime": "primeabgb",
    "pc-studio": "pcstudio",
    "scl": "sclgaming",
    "tps": "tpstech",
    "vishalperipherals": "vishal",
}

app = typer.Typer(
    name="pcforge-scrape",
    help="scraper CLI for fetching, normalizing, and ingesting Indian PC hardware listings.",
    no_args_is_help=True,
)
normalize_app = typer.Typer(
    name="pcforge-normalize",
    help="title normalization, attribute extraction, and match key diagnostic CLI.",
    invoke_without_command=True,
    no_args_is_help=True,
)

console = Console()


@app.callback()
def _main_callback() -> None:
    """scraper CLI."""


def resolve_retailer_targets(retailer_args: Optional[List[str]]) -> List[RetailerSlug]:
    """Validate and normalize a list of `--retailer` CLI arguments, or default to all sources."""
    if not retailer_args:
        return [cast(RetailerSlug, k) for k in SCRAPER_CLASSES]

    resolved: List[RetailerSlug] = []
    for raw_arg in retailer_args:
        for token in raw_arg.split(","):
            candidate = token.strip().lower()
            if not candidate:
                continue
            normalized = RETAILER_ALIASES.get(candidate, candidate)
            if normalized not in SCRAPER_CLASSES:
                raise typer.BadParameter(
                    f"Unknown retailer '{candidate}'. Choices: {', '.join(SCRAPER_CLASSES)}"
                )
            slug = cast(RetailerSlug, normalized)
            if slug not in resolved:
                resolved.append(slug)

    if not resolved:
        return [cast(RetailerSlug, k) for k in SCRAPER_CLASSES]
    return resolved


def _render_database_report(report: DatabaseReport) -> None:
    """Render last scrape runs, per-retailer match/affiliate stats, and catalog rollups."""
    runs_table = Table(title="Last Scrape Runs per Source")
    runs_table.add_column("Retailer", style="cyan")
    runs_table.add_column("Run ID", justify="right")
    runs_table.add_column("Status", style="bold")
    runs_table.add_column("Found", justify="right")
    runs_table.add_column("OK", justify="right", style="green")
    runs_table.add_column("Failed", justify="right", style="red")
    runs_table.add_column("Finished At", style="dim")
    runs_table.add_column("Error / Notes", style="yellow")

    for r in report.last_runs:
        finished_str = r.finished_at.strftime("%Y-%m-%d %H:%M:%S") if r.finished_at else "—"
        runs_table.add_row(
            r.retailer_slug,
            str(r.run_id) if r.run_id is not None else "—",
            r.status or "NEVER",
            str(r.items_found),
            str(r.items_ok),
            str(r.items_failed),
            finished_str,
            r.error or "",
        )
    console.print(runs_table)

    stats_table = Table(title="Listings, Match Status & Affiliate Link Stats per Source")
    stats_table.add_column("Retailer", style="cyan")
    stats_table.add_column("Total", justify="right")
    stats_table.add_column("MATCHED", justify="right", style="green")
    stats_table.add_column("REVIEW", justify="right", style="yellow")
    stats_table.add_column("UNMATCHED", justify="right", style="red")
    stats_table.add_column("Affiliated", justify="right", style="bold green")
    stats_table.add_column("Plain URL", justify="right", style="dim")

    total_listings = 0
    total_matched = 0
    total_review = 0
    total_unmatched = 0
    total_affiliated = 0
    total_plain = 0

    for s in report.retailer_stats:
        total_listings += s.total_listings
        total_matched += s.matched_count
        total_review += s.review_count
        total_unmatched += s.unmatched_count
        total_affiliated += s.affiliated_count
        total_plain += s.plain_count
        stats_table.add_row(
            s.retailer_slug,
            str(s.total_listings),
            str(s.matched_count),
            str(s.review_count),
            str(s.unmatched_count),
            str(s.affiliated_count),
            str(s.plain_count),
        )

    stats_table.add_section()
    stats_table.add_row(
        "TOTAL",
        str(total_listings),
        str(total_matched),
        str(total_review),
        str(total_unmatched),
        str(total_affiliated),
        str(total_plain),
        style="bold",
    )
    console.print(stats_table)

    catalog_table = Table(title="Canonical Product Catalog Summary")
    catalog_table.add_column("Metric", style="cyan")
    catalog_table.add_column("Count", justify="right", style="bold green")
    catalog_table.add_row("Total Canonical Products", str(report.total_products))
    catalog_table.add_row("Products with In-Stock Best Price", str(report.priced_products))
    catalog_table.add_row("Products Offered by >= 2 Sources", str(report.multi_store_products))
    console.print(catalog_table)


@app.command("run")
def run_command(
    retailer: Annotated[
        Optional[List[str]],
        typer.Option(
            "--retailer",
            "-r",
            help=(
                "One or more retailer slugs (e.g. --retailer amazon --retailer computech). "
                "Omit to run all sources."
            ),
        ),
    ] = None,
    category: Annotated[
        Optional[str],
        typer.Option(
            "--category",
            "-c",
            help="Category slug (cpu, gpu, motherboard, ram, psu, case, ssd, hdd, cooler, monitor, fan, keyboard, mouse, headset, controller)",
        ),
    ] = None,
    group: Annotated[
        Optional[str],
        typer.Option(
            "--group",
            "-g",
            help="Category group: 'parts' (CPU, GPU, motherboard, RAM, PSU, case, SSD, HDD, cooler) or 'peripherals' (monitor, fan, keyboard, mouse, headset, controller)",
        ),
    ] = None,
    run_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Explicitly run all registered sources across all categories (default when --retailer is omitted)",
        ),
    ] = False,
    jitter_max_seconds: Annotated[
        int,
        typer.Option(
            "--jitter-max-seconds",
            help="Optional maximum randomized sleep delay in seconds before starting ingestion",
        ),
    ] = 0,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch, normalize, and match without writing to PostgreSQL"),
    ] = False,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", "-o", help="Write ScrapedListing output as JSONL to this path"),
    ] = None,
    use_fixtures: Annotated[
        bool,
        typer.Option(
            "--fixtures",
            help="Read from local saved fixtures (scraper/tests/fixtures) instead of live HTTP",
        ),
    ] = False,
) -> None:
    """Fetch, normalize, match, and ingest listings for specified `--retailer` targets (or all sources if omitted)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if run_all or not retailer:
        retailer_slugs = resolve_retailer_targets(None)
    else:
        retailer_slugs = resolve_retailer_targets(retailer)

    categories: List[CategorySlug]
    if group:
        if group not in CATEGORY_GROUPS:
            raise typer.BadParameter(f"Unknown group '{group}'. Choices: {', '.join(CATEGORY_GROUPS)}")
        if category:
            raise typer.BadParameter("Use either --group or --category, not both.")
        categories = list(CATEGORY_GROUPS[group])
    elif category:
        if category not in ALL_CATEGORIES:
            raise typer.BadParameter(
                f"Unknown category '{category}'. Choices: {', '.join(ALL_CATEGORIES)}"
            )
        categories = [cast(CategorySlug, category)]
    elif use_fixtures:
        categories = list(CORE_CATEGORIES)
    else:
        categories = list(ALL_CATEGORIES)

    if jitter_max_seconds > 0:
        delay_sec = random.randint(0, jitter_max_seconds)
        logger.info("Starting randomized delay sleep window (%ds)...", delay_sec)
        time.sleep(delay_sec)

    logger.info(
        "Routing ingestion for retailers=%s across categories=%s (dry_run=%s)",
        retailer_slugs,
        categories,
        dry_run,
    )

    if not dry_run:
        pipeline_res = run_ingestion_pipeline(
            retailers=retailer_slugs,
            categories=categories,
            use_fixtures=use_fixtures,
        )
        cl = pipeline_res.cuelinks_startup
        console.print(
            f"[bold cyan]Cuelinks Startup Check:[/bold cyan] ping_ok={cl.ping_ok} "
            f"currency={cl.currency or 'N/A'} campaigns_checked={len(cl.campaigns)}"
        )
        _render_database_report(pipeline_res.database_report)

        if out is not None:
            all_ingested: List[ScrapedListing] = []
            for ro in pipeline_res.retailer_outcomes:
                all_ingested.extend(ro.listings)
            out.parent.mkdir(parents=True, exist_ok=True)
            lines = [item.model_dump_json() for item in all_ingested]
            out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            console.print(f"[bold green]Wrote {len(all_ingested)} listings to {out}[/bold green]")
        return

    targets = load_targets()
    fixtures_root = (SCRAPER_DIR / "tests" / "fixtures") if use_fixtures else None

    collected: List[ScrapedListing] = []
    summary_table = Table(title="Scrape Summary (DRY RUN)")
    summary_table.add_column("Retailer", style="cyan")
    summary_table.add_column("Category", style="magenta")
    summary_table.add_column("Quota", justify="right")
    summary_table.add_column("Fetched", justify="right", style="green")
    summary_table.add_column("In Stock", justify="right")
    summary_table.add_column("Skipped", justify="right", style="dim")
    summary_table.add_column("Status / Notes", style="yellow")

    for r_slug in retailer_slugs:
        source = get_retailer_source(
            r_slug,
            targets=targets,
            fixtures_root=fixtures_root,
        )
        try:
            for cat in categories:
                if not source.supports(cat):
                    continue
                quota = targets.get_amazon_quota(cat) if r_slug == "amazon" else targets.get_quota(cat)
                seeds = targets.get_seeds(cat)
                items = source.fetch_category(cat, quota=quota, seeds=seeds)
                in_stock_count = sum(1 for item in items if item.in_stock)
                skipped_count = source.last_skipped_counts.get(cat, 0)
                collected.extend(items)
                status_note = source.last_category_notes.get(cat) or (
                    "OK (quota met)"
                    if len(items) >= quota
                    else f"Shortfall ({len(items)}/{quota}): category feed exhausted"
                )
                summary_table.add_row(
                    r_slug,
                    cat,
                    str(quota),
                    str(len(items)),
                    str(in_stock_count),
                    str(skipped_count),
                    status_note,
                )
        finally:
            close_fn = getattr(source, "close", None)
            if callable(close_fn):
                close_fn()

    console.print(summary_table)

    if collected:
        matcher = ProductMatcher()
        report = matcher.match_batch(collected)

        match_table = Table(title="Normalization & Matching Report")
        match_table.add_column("Metric", style="cyan")
        match_table.add_column("Count", justify="right", style="bold green")
        match_table.add_row("Total Raw Listings Processed", str(report.total_listings))
        match_table.add_row(
            "MATCHED Listings (Total)",
            f"{report.matched_count} (MPN={report.matched_by_mpn}, KEY={report.matched_by_key})",
        )
        match_table.add_row("REVIEW Listings", str(report.review_count))
        match_table.add_row("UNMATCHED Listings", str(report.unmatched_count))
        match_table.add_row("Unique Canonical Products Created", str(report.total_products))
        match_table.add_row("Products Offered by >= 2 Sources", str(report.multi_source_products))
        console.print(match_table)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [item.model_dump_json() for item in collected]
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        console.print(f"[bold green]Wrote {len(collected)} listings to {out}[/bold green]")


@app.command("targeted")
def targeted_command(
    retailer: Annotated[
        Optional[List[str]],
        typer.Option(
            "--retailer",
            "-r",
            help="Stores to search (vedant, mdcomputers, amazon). Omit for all of them.",
        ),
    ] = None,
    budget: Annotated[
        Optional[int],
        typer.Option("--budget", "-b", help="Max products to look up per store this run (default: per-store defaults)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Search and report, but write nothing to the database."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Print every search as it runs.")] = False,
) -> None:
    """Search stores for known products they don't list yet, to add more cross-store comparisons."""
    from pcforge_scraper.targeted import SEARCHABLE_RETAILERS, run_targeted_lookup

    wanted = None
    if retailer:
        wanted = [RETAILER_ALIASES.get(r.strip().lower(), r.strip().lower()) for raw in retailer for r in raw.split(",") if r.strip()]
        unknown = [r for r in wanted if r not in SEARCHABLE_RETAILERS]
        if unknown:
            raise typer.BadParameter(f"Not searchable: {', '.join(unknown)}. Choices: {', '.join(SEARCHABLE_RETAILERS)}")
    budgets = {r: budget for r in SEARCHABLE_RETAILERS} if budget else None

    report = run_targeted_lookup(
        retailers=wanted,
        budgets=budgets,
        dry_run=dry_run,
        on_progress=(lambda msg: console.print(f"[dim]{msg}[/dim]")) if verbose else None,
    )

    table = Table(title=f"Targeted lookup{' (DRY RUN)' if dry_run else ''}")
    for col, style in (("Store", "cyan"), ("Due", ""), ("Looked up", ""), ("Searches", ""), ("Found", "green"),
                       ("Price refresh", ""), ("Bonus matches", "green"), ("Failed", "red"), ("Time", "dim")):
        table.add_column(col, style=style or None, justify="left" if col == "Store" else "right")
    for run in report.stores:
        table.add_row(
            run.retailer,
            str(run.due_total),
            str(run.targets),
            str(run.queries),
            str(run.found),
            str(run.refreshed),
            str(run.bonus),
            str(run.requests_failed),
            f"{run.seconds:.0f}s",
        )
    console.print(table)
    for run in report.stores:
        for err in run.errors[:5]:
            console.print(f"[yellow]{run.retailer}: {err}[/yellow]")
    if not report.stores:
        console.print("[yellow]No searchable stores available (check Amazon credentials).[/yellow]")


@app.command("snapshot")
def snapshot_command() -> None:
    """Record today's lowest price per product (price history + alerts). Runs automatically after each scrape."""
    from pcforge_scraper.db import record_price_snapshots

    with connect_db() as conn:
        written = record_price_snapshots(conn)
    if written:
        console.print(f"[green]Recorded today's prices for {written} products.[/green]")
    else:
        console.print("[yellow]Nothing recorded (price_snapshot table not created yet, or no listed products).[/yellow]")


@app.command("report")
def report_command() -> None:
    """Print last scrape runs, per-retailer match stats, and affiliated vs plain counts from Neon."""
    with connect_db() as conn:
        report = fetch_database_report(conn)
    _render_database_report(report)


def _run_explain(
    title: str,
    category: Optional[str] = None,
    mpn: Optional[str] = None,
    brand: Optional[str] = None,
    price: Optional[str] = None,
) -> None:
    cat: CategorySlug = (
        cast(CategorySlug, category)
        if category and category in ALL_CATEGORIES
        else infer_category_from_title(title)
    )
    ext = extract_attributes(cat, title, raw_brand=brand, raw_mpn=mpn)
    key = build_match_key(ext)
    canon_name, canon_slug = build_canonical_name_and_slug(ext)
    status = "MATCHED" if key is not None else "REVIEW"
    notes = (
        "; ".join(ext.conflict_notes)
        if ext.conflict_notes
        else (
            f"Missing required {cat} field(s): {', '.join(ext.missing_fields)}"
            if ext.missing_fields
            else None
        )
    )
    output: dict[str, object] = {
        "raw_title": title,
        "category": cat,
        "cleaned_title": ext.cleaned_title,
        "brand_slug": ext.brand_slug,
        "mpn": ext.mpn,
        "attributes": ext.attributes,
        "match_key": key,
        "canonical_name": canon_name,
        "canonical_slug": canon_slug,
        "status": status,
        "notes": notes,
    }
    if price is not None:
        output["price_paise"] = parse_price_paise(price)
    console.print(output)


@normalize_app.callback(invoke_without_command=True)
def normalize_main(
    ctx: typer.Context,
    explain_title: Annotated[
        Optional[str],
        typer.Option("--explain", "-e", help="Raw product title to normalize and explain"),
    ] = None,
    category: Annotated[
        Optional[str],
        typer.Option(
            "--category",
            "-c",
            help="Optional category slug (cpu, gpu, motherboard, ram, psu, case)",
        ),
    ] = None,
    mpn: Annotated[
        Optional[str],
        typer.Option("--mpn", help="Optional raw MPN"),
    ] = None,
    brand: Annotated[
        Optional[str],
        typer.Option("--brand", help="Optional raw brand"),
    ] = None,
    price: Annotated[
        Optional[str],
        typer.Option("--price", help="Optional raw price string"),
    ] = None,
) -> None:
    """Normalize a title, extract per-category attributes, and print the match key."""
    if explain_title is not None:
        _run_explain(explain_title, category=category, mpn=mpn, brand=brand, price=price)
    elif ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
