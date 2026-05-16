"""Round-trip tests for the SQLite persistence layer."""

from __future__ import annotations

from homeagent import db
from homeagent.models import Analysis, Listing, Project, RERAEntry, Report


def test_upsert_and_read_project(tmp_db):
    pid = db.upsert_project(tmp_db, Project(name="Skyline Heights", locality="Kharadi", builder="ABC"))
    pid2 = db.upsert_project(tmp_db, Project(name="Skyline Heights", locality="Kharadi", rera_id="P52100012345"))
    assert pid == pid2  # upsert keyed on (name, locality)

    fetched = db.get_project(tmp_db, pid)
    assert fetched is not None
    assert fetched.name == "Skyline Heights"
    assert fetched.builder == "ABC"  # preserved via COALESCE
    assert fetched.rera_id == "P52100012345"  # added on second upsert


def test_upsert_and_read_listing(tmp_db):
    listing = Listing(
        portal="nobroker",
        portal_listing_id="NB-12345",
        url="https://nobroker.in/property/abc",
        title="3 BHK in Kharadi",
        price_inr=12_500_000,
        carpet_area_sqft=1100.0,
        bhk=3.0,
        locality="Kharadi",
        status="resale",
        raw={"amenities": ["pool", "gym"]},
    )
    lid = db.upsert_listing(tmp_db, listing)

    fetched = db.get_listing(tmp_db, lid)
    assert fetched is not None
    assert fetched.price_inr == 12_500_000
    assert fetched.raw == {"amenities": ["pool", "gym"]}
    assert abs(fetched.price_per_sqft - (12_500_000 / 1100.0)) < 0.01


def test_listing_upsert_idempotent_and_updates(tmp_db):
    base = Listing(portal="nobroker", portal_listing_id="NB-1", url="https://x/1", title="A")
    lid = db.upsert_listing(tmp_db, base)

    updated = Listing(portal="nobroker", portal_listing_id="NB-1", url="https://x/1", title="A v2", price_inr=999)
    lid2 = db.upsert_listing(tmp_db, updated)

    assert lid == lid2
    fetched = db.get_listing(tmp_db, lid)
    assert fetched.title == "A v2"
    assert fetched.price_inr == 999


def test_list_listings_unverified_filter(tmp_db):
    a = Listing(portal="nobroker", portal_listing_id="A", url="https://x/a", title="A")
    b = Listing(portal="nobroker", portal_listing_id="B", url="https://x/b", title="B")
    aid = db.upsert_listing(tmp_db, a)
    db.upsert_listing(tmp_db, b)
    db.upsert_analysis(tmp_db, Analysis(listing_id=aid, check_name="rera_match", verdict="pass", score=1.0))

    unverified = db.list_listings(tmp_db, unverified_only=True)
    assert {l.portal_listing_id for l in unverified} == {"B"}


def test_upsert_rera(tmp_db):
    entry = RERAEntry(rera_id="P52100099999", project_name="Demo", promoter="ACME", status="Registered")
    eid = db.upsert_rera(tmp_db, entry)
    fetched = db.get_rera(tmp_db, "P52100099999")
    assert fetched is not None
    assert fetched.id == eid
    assert fetched.status == "Registered"


def test_upsert_analysis_replaces(tmp_db):
    lid = db.upsert_listing(tmp_db, Listing(portal="x", portal_listing_id="1", url="https://x/1", title="T"))
    db.upsert_analysis(tmp_db, Analysis(listing_id=lid, check_name="pricing", verdict="warn", score=0.4))
    db.upsert_analysis(tmp_db, Analysis(listing_id=lid, check_name="pricing", verdict="pass", score=0.9))

    analyses = db.get_analyses(tmp_db, lid)
    assert len(analyses) == 1
    assert analyses[0].verdict == "pass"
    assert analyses[0].score == 0.9


def test_insert_report(tmp_db):
    rid = db.insert_report(
        tmp_db,
        Report(
            criteria_snapshot={"bhk": [3], "budget_cr": [1.0, 1.5]},
            ranked_listing_ids=[1, 2, 3],
            markdown="# Report",
        ),
    )
    assert rid > 0
