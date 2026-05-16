"""LangGraph orchestration for the homeagent pipeline.

Graph:
    ingest → dedupe → enrich_projects → run_checks → score_and_rank → render_report

Each node mutates AgentState in place. Checks within `run_checks` are pulled from the registry
so new checks added under `homeagent/verification/` are picked up automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from langgraph.graph import END, StateGraph

from homeagent import db
from homeagent.agent.state import AgentState
from homeagent.config import SearchCriteria, Settings, load_criteria
from homeagent.models import Analysis, Listing
from homeagent.scrapers.base import RawListing, SearchCriteria as ScraperCriteria
from homeagent.verification.registry import discover_checks, list_checks, run_check

log = logging.getLogger(__name__)


# --------------- Scraper registry ---------------


def _scraper_instances(portal: str) -> list:
    """Return scraper instances matching the requested portal selector."""
    from homeagent.scrapers.nobroker import NoBrokerScraper

    all_scrapers = {"nobroker": NoBrokerScraper()}

    # Magicbricks and 99acres slots; populated when their modules land.
    try:
        from homeagent.scrapers.magicbricks import MagicbricksScraper  # type: ignore

        all_scrapers["magicbricks"] = MagicbricksScraper()
    except ImportError:
        pass
    try:
        from homeagent.scrapers.ninetynineacres import NinetyNineAcresScraper  # type: ignore

        all_scrapers["99acres"] = NinetyNineAcresScraper()
    except ImportError:
        pass

    if portal == "all":
        return list(all_scrapers.values())
    if portal not in all_scrapers:
        raise ValueError(f"unknown portal {portal!r}; have {sorted(all_scrapers)}")
    return [all_scrapers[portal]]


def _criteria_to_scraper(c: SearchCriteria) -> ScraperCriteria:
    return ScraperCriteria(
        city=c.city,
        bhk=c.bhk,
        budget_min_inr=c.budget_min_inr,
        budget_max_inr=c.budget_max_inr,
        localities=c.localities,
        property_status=c.property_status,
    )


def _raw_to_listing(raw: RawListing) -> Listing:
    return Listing(
        portal=raw.portal,
        portal_listing_id=raw.portal_listing_id,
        url=raw.url,
        title=raw.title,
        price_inr=raw.price_inr,
        carpet_area_sqft=raw.carpet_area_sqft,
        built_up_area_sqft=raw.built_up_area_sqft,
        bhk=raw.bhk,
        locality=raw.locality,
        status=raw.status if raw.status in {"new", "resale", "unknown"} else "unknown",
        raw={
            "project_name": raw.project_name,
            "builder": raw.builder,
            **raw.extra,
        },
    )


# --------------- Graph nodes ---------------


def node_ingest(state: AgentState) -> AgentState:
    portal = state.get("portal", "all")
    limit = state.get("limit", 10)
    criteria = state.get("criteria") or load_criteria()
    state["criteria"] = criteria

    scraper_criteria = _criteria_to_scraper(criteria)
    new_ids: list[int] = []
    for scraper in _scraper_instances(portal):
        log.info("ingesting from %s (limit=%d)", scraper.name, limit)
        try:
            raws: Iterable[RawListing] = scraper.search(scraper_criteria, limit=limit)
            with db.connect() as conn:
                for raw in raws:
                    listing = _raw_to_listing(raw)
                    lid = db.upsert_listing(conn, listing)
                    new_ids.append(lid)
        except Exception as e:
            log.warning("ingest failed for %s: %s", scraper.name, e)

    state["listing_ids"] = new_ids
    state.setdefault("summary", {})["ingested"] = len(new_ids)
    return state


def node_dedupe(state: AgentState) -> AgentState:
    """Cross-portal dedupe: same (project_name+locality+area±5%+price±5%) collapses to one.

    We don't delete duplicates yet — the next phase can mark them. For MVP, we just log.
    """
    ids = state.get("listing_ids", [])
    with db.connect() as conn:
        listings = [db.get_listing(conn, i) for i in ids if i is not None]
    keys = {}
    dups = 0
    for l in listings:
        if not l:
            continue
        pn = (l.raw.get("project_name") if l.raw else None) or ""
        loc = (l.locality or "").lower()
        area_bucket = round((l.built_up_area_sqft or l.carpet_area_sqft or 0) / 50) * 50
        price_bucket = round((l.price_inr or 0) / 500_000) * 500_000
        key = (pn.lower(), loc, area_bucket, price_bucket)
        if key in keys and pn:
            dups += 1
        else:
            keys[key] = l.id
    state.setdefault("summary", {})["duplicates"] = dups
    log.info("dedupe: %d duplicates among %d listings", dups, len(listings))
    return state


def node_run_checks(state: AgentState) -> AgentState:
    """Run every registered check against every listing in scope."""
    discover_checks()
    criteria = state.get("criteria") or load_criteria()
    ids = state.get("listing_ids") or []

    if not ids:
        with db.connect() as conn:
            ids = [l.id for l in db.list_listings(conn, unverified_only=True) if l.id is not None]
        state["listing_ids"] = ids

    checks = list_checks()
    log.info("running %d checks on %d listings", len(checks), len(ids))

    verified = 0
    with db.connect() as conn:
        for lid in ids:
            listing = db.get_listing(conn, lid)
            if not listing:
                continue
            for check in checks:
                try:
                    result = check.fn(
                        listing=listing,
                        project=None,
                        context={"criteria": criteria},
                    )
                except Exception as e:
                    log.warning("check %s failed for listing %d: %s", check.name, lid, e)
                    continue
                db.upsert_analysis(
                    conn,
                    Analysis(
                        listing_id=lid,
                        check_name=check.name,
                        verdict=result.verdict,
                        score=result.score,
                        evidence=result.evidence,
                    ),
                )
            verified += 1

    state.setdefault("summary", {})["verified"] = verified
    return state


def node_score_and_rank(state: AgentState) -> AgentState:
    """Compute composite score per listing using criteria.scoring_weights."""
    criteria = state.get("criteria") or load_criteria()
    weights = criteria.scoring_weights

    ids = state.get("listing_ids") or []
    if not ids:
        with db.connect() as conn:
            ids = [l.id for l in db.list_listings(conn) if l.id is not None]

    scored: list[tuple[int, float]] = []
    with db.connect() as conn:
        for lid in ids:
            analyses = db.get_analyses(conn, lid)
            if not analyses:
                continue
            total = 0.0
            wsum = 0.0
            for a in analyses:
                w = weights.get(a.check_name, 0.1)
                total += w * a.score
                wsum += w
            composite = total / wsum if wsum else 0.0
            scored.append((lid, composite))

    scored.sort(key=lambda x: -x[1])
    state["listing_ids"] = [lid for lid, _ in scored]
    state.setdefault("summary", {})["ranked"] = len(scored)
    return state


def node_render_report(state: AgentState) -> AgentState:
    from homeagent.reporting.render import render_report_markdown

    top_n = state.get("top_n", 10)
    ids = (state.get("listing_ids") or [])[:top_n]
    out = state.get("report_path") or str(Settings().reports_dir / "latest.md")

    md = render_report_markdown(ids, criteria=state.get("criteria"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(md, encoding="utf-8")

    from homeagent.models import Report
    with db.connect() as conn:
        db.insert_report(
            conn,
            Report(
                criteria_snapshot=(state.get("criteria") or load_criteria()).model_dump(),
                ranked_listing_ids=ids,
                markdown=md,
            ),
        )

    state["report_path"] = out
    state.setdefault("summary", {})["report"] = out
    return state


# --------------- Graph wiring + public runners ---------------


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("ingest", node_ingest)
    g.add_node("dedupe", node_dedupe)
    g.add_node("run_checks", node_run_checks)
    g.add_node("score_and_rank", node_score_and_rank)
    g.add_node("render_report", node_render_report)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "dedupe")
    g.add_edge("dedupe", "run_checks")
    g.add_edge("run_checks", "score_and_rank")
    g.add_edge("score_and_rank", "render_report")
    g.add_edge("render_report", END)
    return g.compile()


def run_ingest(portal: str = "all", limit: int = 10) -> None:
    state: AgentState = {"portal": portal, "limit": limit}
    out = node_ingest(state)
    print(f"ingested: {out.get('summary', {}).get('ingested', 0)} listings")


def run_verify(listing_id: int | None = None, all_unverified: bool = False) -> None:
    state: AgentState = {}
    if listing_id:
        state["listing_ids"] = [listing_id]
    out = node_run_checks(state)
    print(f"verified: {out.get('summary', {}).get('verified', 0)} listings")


def run_report(top: int = 10, out: str = "reports/latest.md") -> str:
    state: AgentState = {"top_n": top, "report_path": out}
    node_score_and_rank(state)
    node_render_report(state)
    return state.get("report_path", out)


def run_pipeline(portal: str = "all", limit: int = 10, top: int = 10) -> dict:
    graph = build_graph()
    state: AgentState = {"portal": portal, "limit": limit, "top_n": top}
    result = graph.invoke(state)
    return dict(result.get("summary", {}))
