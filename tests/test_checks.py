"""Tests for the verification registry and individual checks."""

from __future__ import annotations

import pytest

from homeagent import db
from homeagent.config import Proximity, SearchCriteria
from homeagent.models import Listing, RERAEntry
from homeagent.verification.registry import (
    CheckResult,
    discover_checks,
    list_checks,
    register_check,
    run_check,
)


@pytest.fixture(scope="module", autouse=True)
def _discover_once():
    """Discover all check modules exactly once for this test module."""
    discover_checks()
    yield


def test_registry_register_and_run():
    @register_check(name="dummy_for_test", weight=0.1)
    def _dummy(listing, project, context):
        return CheckResult(verdict="pass", score=1.0, evidence={"echo": listing.title})

    names = [c.name for c in list_checks()]
    assert "dummy_for_test" in names

    result = run_check("dummy_for_test", Listing(portal="x", portal_listing_id="1", url="https://x", title="T"))
    assert result.verdict == "pass"
    assert result.evidence == {"echo": "T"}


def test_discover_checks_finds_builtins():
    names = {c.name for c in list_checks()}
    assert {"pricing", "rera_match", "legal", "locality_fit"}.issubset(names)


def test_rera_match_check_pass_when_registered(tmp_path, monkeypatch):
    """RERA check classifies a Registered project as pass."""
    discover_checks()
    listing = Listing(
        portal="nobroker",
        portal_listing_id="1",
        url="https://x",
        title="3 BHK in Skyline Heights, Kharadi",
        bhk=3.0,
        locality="Kharadi",
        raw={"project_name": "Skyline Heights", "builder": "Skyline Developers Pvt Ltd"},
    )
    rera_entry = RERAEntry(
        rera_id="P52100012345",
        project_name="Skyline Heights",
        promoter="Skyline Developers Pvt Ltd",
        status="Registered",
        complaints_count=1,
    )
    result = run_check("rera_match", listing, context={"rera_entry": rera_entry})
    assert result.verdict == "pass"
    assert result.score == 1.0
    assert result.evidence["rera_id"] == "P52100012345"


def test_rera_match_check_fail_when_not_found():
    discover_checks()
    listing = Listing(
        portal="nobroker",
        portal_listing_id="1",
        url="https://x",
        title="3 BHK in Unknown Project, Wagholi",
        raw={"project_name": "Unknown Project"},
    )
    # Inject None by patching lookup_project
    from homeagent.verification import rera_check

    rera_check.rera.lookup_project = lambda name, **kw: None
    result = run_check("rera_match", listing)
    assert result.verdict == "fail"
    assert result.score == 0.0


def test_locality_fit_pass(tmp_path):
    discover_checks()
    criteria = SearchCriteria(
        city="Pune",
        bhk=[3.0],
        budget_min_cr=1.0,
        budget_max_cr=1.5,
        property_status=["resale"],
        proximity=Proximity(anchor_name="EON", anchor_lat=0, anchor_lng=0, max_distance_km=5),
        localities=["Kharadi", "Wagholi"],
        scoring_weights={"locality_fit": 1.0},
    )
    listing = Listing(portal="x", portal_listing_id="1", url="https://x", title="T", locality="Kharadi")
    result = run_check("locality_fit", listing, context={"criteria": criteria})
    assert result.verdict == "pass"


def test_locality_fit_fail_out_of_whitelist():
    discover_checks()
    criteria = SearchCriteria(
        city="Pune",
        bhk=[3.0],
        budget_min_cr=1.0,
        budget_max_cr=1.5,
        property_status=["resale"],
        proximity=Proximity(anchor_name="EON", anchor_lat=0, anchor_lng=0, max_distance_km=5),
        localities=["Kharadi"],
        scoring_weights={},
    )
    listing = Listing(portal="x", portal_listing_id="1", url="https://x", title="T", locality="Baner")
    result = run_check("locality_fit", listing, context={"criteria": criteria})
    assert result.verdict == "fail"


def test_pricing_check_unknown_with_no_samples(tmp_db, monkeypatch):
    discover_checks()
    # Point the pricing check at our tmp DB by monkeypatching the connect() it imports
    from homeagent.verification import pricing
    from contextlib import contextmanager

    @contextmanager
    def fake_connect():
        yield tmp_db

    monkeypatch.setattr(pricing, "connect", fake_connect)

    listing = Listing(
        portal="x", portal_listing_id="1", url="https://x", title="T",
        price_inr=10_000_000, built_up_area_sqft=1000, locality="Kharadi",
    )
    result = run_check("pricing", listing)
    assert result.verdict == "unknown"
    assert "insufficient" in result.evidence["reason"]


def test_pricing_check_passes_within_band(tmp_db, monkeypatch):
    discover_checks()
    from homeagent.verification import pricing
    from contextlib import contextmanager

    # Seed comparable Kharadi listings around ₹10k/sqft
    for i, pps in enumerate([9500, 10000, 10500, 10200]):
        db.upsert_listing(
            tmp_db,
            Listing(
                portal="x", portal_listing_id=f"seed-{i}", url=f"https://x/{i}", title="T",
                price_inr=pps * 1000, built_up_area_sqft=1000, locality="Kharadi",
            ),
        )

    @contextmanager
    def fake_connect():
        yield tmp_db
    monkeypatch.setattr(pricing, "connect", fake_connect)

    listing = Listing(
        portal="x", portal_listing_id="new", url="https://x/new", title="T",
        price_inr=10_100_000, built_up_area_sqft=1000, locality="Kharadi",
    )
    result = run_check("pricing", listing)
    assert result.verdict == "pass"
    assert 0.9 <= result.evidence["ratio"] <= 1.1


def test_pricing_check_warns_when_overpriced(tmp_db, monkeypatch):
    discover_checks()
    from homeagent.verification import pricing
    from contextlib import contextmanager

    for i, pps in enumerate([9500, 10000, 10500, 10200]):
        db.upsert_listing(
            tmp_db,
            Listing(
                portal="x", portal_listing_id=f"seed-{i}", url=f"https://x/{i}", title="T",
                price_inr=pps * 1000, built_up_area_sqft=1000, locality="Kharadi",
            ),
        )

    @contextmanager
    def fake_connect():
        yield tmp_db
    monkeypatch.setattr(pricing, "connect", fake_connect)

    listing = Listing(
        portal="x", portal_listing_id="new", url="https://x/new", title="T",
        price_inr=15_000_000, built_up_area_sqft=1000, locality="Kharadi",
    )
    result = run_check("pricing", listing)
    assert result.verdict == "warn"
    assert result.evidence["flag"] == "overpriced"


def test_legal_check_uses_injected_result(tmp_path, monkeypatch):
    discover_checks()
    from homeagent.verification import legal

    monkeypatch.setattr(legal, "_cache_path", lambda b: tmp_path / f"legal_{b}.json")

    listing = Listing(
        portal="x", portal_listing_id="1", url="https://x", title="T",
        raw={"builder": "Suspicious Builder Pvt Ltd"},
    )
    injected = {
        "verdict": "warn",
        "score": 0.55,
        "summary": "One delay complaint found.",
        "incidents": [{"title": "Delay", "url": "https://example.com", "severity": "low"}],
    }
    result = run_check("legal", listing, context={"legal_result": injected})
    assert result.verdict == "warn"
    assert result.score == 0.55
    assert result.evidence["summary"].startswith("One delay")
