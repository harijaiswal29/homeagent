"""Tests for the scoring/ranking node and the report renderer (template path)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from homeagent import db
from homeagent.agent.graph import node_score_and_rank
from homeagent.config import Proximity, SearchCriteria
from homeagent.models import Analysis, Listing
from homeagent.reporting.render import render_report_markdown


@pytest.fixture
def fake_criteria():
    return SearchCriteria(
        city="Pune",
        bhk=[2.5, 3.0],
        budget_min_cr=1.0,
        budget_max_cr=1.5,
        property_status=["new", "resale"],
        proximity=Proximity(anchor_name="EON IT Park", anchor_lat=18.55, anchor_lng=73.95, max_distance_km=5),
        localities=["Kharadi"],
        scoring_weights={"rera_match": 0.4, "pricing": 0.25, "legal": 0.2, "locality_fit": 0.15},
    )


@pytest.fixture
def populated_db(tmp_db, monkeypatch):
    """Seed three Kharadi listings with varying check outcomes."""
    listings = [
        # ID 1: strong candidate — RERA pass, price ok, legal pass
        ("strong", 12_000_000, 1200, "Kharadi", "Skyline Heights", "Skyline Devs"),
        # ID 2: mid — RERA pass, overpriced, legal pass
        ("mid", 14_500_000, 1200, "Kharadi", "Marvel Ribera", "Marvel"),
        # ID 3: weak — RERA fail, price ok, legal warn
        ("weak", 11_000_000, 1100, "Kharadi", "Unknown Project", "Unknown Dev"),
    ]
    ids: dict[str, int] = {}
    for ext_id, price, area, loc, proj, builder in listings:
        lid = db.upsert_listing(
            tmp_db,
            Listing(
                portal="nobroker",
                portal_listing_id=ext_id,
                url=f"https://nobroker.in/{ext_id}",
                title=f"3 BHK in {proj}, {loc}",
                price_inr=price,
                built_up_area_sqft=float(area),
                bhk=3.0,
                locality=loc,
                raw={"project_name": proj, "builder": builder},
            ),
        )
        ids[ext_id] = lid

    # Strong
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["strong"], check_name="rera_match", verdict="pass", score=1.0, evidence={"rera_id": "P52100012345"}))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["strong"], check_name="pricing", verdict="pass", score=0.95, evidence={"ratio": 1.02}))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["strong"], check_name="legal", verdict="pass", score=0.9, evidence={"summary": "clean"}))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["strong"], check_name="locality_fit", verdict="pass", score=1.0))

    # Mid
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["mid"], check_name="rera_match", verdict="pass", score=1.0, evidence={"rera_id": "P51800022222"}))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["mid"], check_name="pricing", verdict="warn", score=0.3, evidence={"flag": "overpriced", "ratio": 1.30}))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["mid"], check_name="legal", verdict="pass", score=0.9))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["mid"], check_name="locality_fit", verdict="pass", score=1.0))

    # Weak
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["weak"], check_name="rera_match", verdict="fail", score=0.0, evidence={}))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["weak"], check_name="pricing", verdict="pass", score=0.9))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["weak"], check_name="legal", verdict="warn", score=0.5))
    db.upsert_analysis(tmp_db, Analysis(listing_id=ids["weak"], check_name="locality_fit", verdict="pass", score=1.0))

    # Patch graph + render to use this tmp DB
    from homeagent.agent import graph as graph_mod
    from homeagent import reporting
    from homeagent.reporting import render as render_mod

    @contextmanager
    def fake_connect(*a, **kw):
        yield tmp_db

    monkeypatch.setattr(graph_mod.db, "connect", fake_connect)
    monkeypatch.setattr(render_mod.db, "connect", fake_connect)
    return ids


def test_score_and_rank_orders_correctly(populated_db, fake_criteria):
    state = {"criteria": fake_criteria, "listing_ids": list(populated_db.values())}
    out = node_score_and_rank(state)
    ranked = out["listing_ids"]
    # Strong should outrank mid (no overprice penalty); both should outrank weak (RERA fail)
    assert ranked[0] == populated_db["strong"]
    assert ranked[-1] == populated_db["weak"]


def test_render_template_path_includes_required_sections(populated_db, fake_criteria, monkeypatch):
    # Force the template path (no API key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    state = {"criteria": fake_criteria, "listing_ids": list(populated_db.values())}
    ranked = node_score_and_rank(state)["listing_ids"]
    md = render_report_markdown(ranked, criteria=fake_criteria)

    assert "# Pune Property Shortlist" in md
    assert "## Ranked shortlist" in md
    assert "## Detailed analysis" in md
    assert "Skyline Heights" in md  # strong candidate present
    assert "rera_match" in md  # evidence cited
    assert "Composite" in md or "composite" in md
