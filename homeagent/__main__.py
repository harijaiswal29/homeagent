"""CLI entrypoint: `python -m homeagent <command>`."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="homeagent",
    help="Real Estate AI Agent for Pune house search.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def ingest(
    portal: str = typer.Option("all", help="nobroker|magicbricks|99acres|all"),
    limit: int = typer.Option(10, help="Max listings per portal"),
) -> None:
    """Scrape one or more portals and upsert listings into the DB."""
    from homeagent.agent.graph import run_ingest

    run_ingest(portal=portal, limit=limit)


@app.command()
def verify(
    listing_id: int | None = typer.Option(None, help="Verify a single listing by ID"),
    all_unverified: bool = typer.Option(False, "--all-unverified", help="Verify every unverified listing"),
) -> None:
    """Run all registered checks (RERA, pricing, legal, ...) on listings."""
    from homeagent.agent.graph import run_verify

    run_verify(listing_id=listing_id, all_unverified=all_unverified)


@app.command()
def report(
    top: int = typer.Option(10, help="Number of top-ranked listings to include"),
    out: str = typer.Option("reports/latest.md", help="Where to write the Markdown report"),
) -> None:
    """Generate a ranked Markdown report."""
    from homeagent.agent.graph import run_report

    path = run_report(top=top, out=out)
    console.print(f"[green]Report written to[/green] {path}")


@app.command(name="run-all")
def run_all(
    portal: str = typer.Option("all", help="nobroker|magicbricks|99acres|all"),
    limit: int = typer.Option(10, help="Max listings per portal"),
    top: int = typer.Option(10, help="Top-N ranked listings in the report"),
) -> None:
    """Ingest → verify → report in one go."""
    from homeagent.agent.graph import run_pipeline

    run_pipeline(portal=portal, limit=limit, top=top)


@app.command()
def show(listing_id: int) -> None:
    """Show a single listing with all its analysis results."""
    from homeagent.reporting.render import render_listing_detail

    console.print(render_listing_detail(listing_id))


@app.command()
def status() -> None:
    """Print a snapshot: how many listings, how many verified, top scores."""
    from homeagent import db
    from homeagent.config import load_criteria

    criteria = load_criteria()
    with db.connect() as conn:
        all_listings = db.list_listings(conn)
        unverified = db.list_listings(conn, unverified_only=True)

    table = Table(title="homeagent status")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("DB listings", str(len(all_listings)))
    table.add_row("Unverified", str(len(unverified)))
    table.add_row(
        "Search budget",
        f"₹{criteria.budget_min_cr:.1f}–{criteria.budget_max_cr:.1f} Cr",
    )
    table.add_row("BHK", ", ".join(str(b) for b in criteria.bhk))
    table.add_row("Localities", ", ".join(criteria.localities))
    console.print(table)


@app.command()
def demo() -> None:
    """Seed the DB from `tests/fixtures/` and run verify+report end-to-end.

    Use this to validate the full pipeline without depending on live portals.
    The legal check will fall back to 'unknown' verdicts unless ANTHROPIC_API_KEY is set.
    """
    from homeagent import db
    from homeagent.agent.graph import node_render_report, node_run_checks, node_score_and_rank
    from homeagent.config import load_criteria
    from homeagent.scrapers.magicbricks import MagicbricksScraper
    from homeagent.scrapers.ninetynineacres import NinetyNineAcresScraper
    from homeagent.scrapers.nobroker import NoBrokerScraper

    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    console.print(f"[cyan]Seeding from[/cyan] {fixtures}")

    # Seed a few MahaRERA entries so the rera_match check finds them in cache
    # rather than trying live (which needs Playwright + network).
    from homeagent.models import RERAEntry

    seed_rera = [
        RERAEntry(
            rera_id="P52100012345", project_name="Skyline Heights",
            promoter="Skyline Developers Pvt Ltd", status="Registered",
            completion_date="2026-12-31", complaints_count=1,
        ),
        RERAEntry(
            rera_id="P51800022222", project_name="Marvel Ribera",
            promoter="Marvel Realtors", status="Registered",
            completion_date="2025-06-30", complaints_count=0,
        ),
    ]
    with db.connect() as conn:
        for e in seed_rera:
            db.upsert_rera(conn, e)

    parsers = [
        (NoBrokerScraper(), fixtures / "nobroker_search.html", "parse_search_results"),
        (MagicbricksScraper(), fixtures / "magicbricks_search.html", "parse_search_results"),
        (NinetyNineAcresScraper(), fixtures / "99acres_search.html", "parse_search_results"),
    ]

    inserted_ids: list[int] = []
    with db.connect() as conn:
        for scraper, fixture_path, method in parsers:
            if not fixture_path.exists():
                console.print(f"  [yellow]skip[/yellow] {scraper.name}: no fixture")
                continue
            html = fixture_path.read_text()
            results = getattr(scraper, method)(html)
            for raw in results:
                from homeagent.agent.graph import _raw_to_listing

                lid = db.upsert_listing(conn, _raw_to_listing(raw))
                inserted_ids.append(lid)
            console.print(f"  [green]ok[/green] {scraper.name}: {len(results)} listings")

    console.print(f"[cyan]Seeded {len(inserted_ids)} listings.[/cyan]")

    state = {"listing_ids": inserted_ids, "criteria": load_criteria(), "top_n": 10}
    state = node_run_checks(state)
    console.print(f"[cyan]Verified {state['summary'].get('verified', 0)} listings.[/cyan]")
    state = node_score_and_rank(state)
    state = node_render_report(state)
    console.print(f"[green]Report written to[/green] {state['report_path']}")


if __name__ == "__main__":
    app()
