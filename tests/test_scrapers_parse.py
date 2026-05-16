"""Offline parse tests against HTML fixtures. These do NOT hit the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeagent.scrapers.base import SearchCriteria
from homeagent.scrapers.nobroker import NoBrokerScraper, _parse_area, _parse_bhk, _parse_inr


# ---------- pure helper tests ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("₹1.25 Cr", 12_500_000),
        ("95 Lakh", 9_500_000),
        ("95 Lac", 9_500_000),
        ("1,25,00,000", 12_500_000),
        ("nonsense", None),
        ("", None),
        # Regression: leading digits from a title like "3 BHK Apartment..." must NOT be
        # interpreted as a 3-rupee price.
        ("3 BHK Apartment for Sale in Geras Island of Joy, Wagholi Pune", None),
        ("2.5 BHK Flat", None),
    ],
)
def test_parse_inr(text, expected):
    assert _parse_inr(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1,250 sq ft", 1250.0),
        ("1100 sqft", 1100.0),
        ("Built up: 950 sft", 950.0),
        ("no area here", None),
    ],
)
def test_parse_area(text, expected):
    assert _parse_area(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3 BHK Apartment", 3.0),
        ("2.5BHK in Kharadi", 2.5),
        ("Studio for sale", None),
    ],
)
def test_parse_bhk(text, expected):
    assert _parse_bhk(text) == expected


# ---------- fixture-backed parser tests ----------


def test_nobroker_search_parse(fixtures_dir: Path):
    html = (fixtures_dir / "nobroker_search.html").read_text()
    scraper = NoBrokerScraper()
    results = scraper.parse_search_results(html)

    assert len(results) == 3
    by_id = {r.portal_listing_id: r for r in results}

    skyline = by_id["8a808fdf90a1b2c3"]
    assert skyline.title.startswith("3 BHK")
    assert skyline.price_inr == 12_500_000
    assert skyline.built_up_area_sqft == 1250.0
    assert skyline.bhk == 3.0
    assert skyline.locality == "Kharadi"
    assert skyline.url.endswith("8a808fdf90a1b2c3")

    marvel = by_id["9b919eef01b2c3d4"]
    assert marvel.bhk == 2.5
    assert marvel.price_inr == 11_000_000


def test_nobroker_detail_parse(fixtures_dir: Path):
    html = (fixtures_dir / "nobroker_detail.html").read_text()
    url = "https://www.nobroker.in/property/sale/pune/Kharadi/detail/8a808fdf90a1b2c3"
    scraper = NoBrokerScraper()
    raw = scraper.parse_detail(html, url=url)

    assert raw.portal == "nobroker"
    assert raw.portal_listing_id == "8a808fdf90a1b2c3"
    assert raw.price_inr == 12_500_000
    assert raw.built_up_area_sqft == 1250.0
    assert raw.bhk == 3.0
    assert raw.locality == "Kharadi"


def test_nobroker_matches_filters_kharadi_3bhk():
    scraper = NoBrokerScraper()
    criteria = SearchCriteria(
        city="Pune",
        bhk=[2.5, 3.0],
        budget_min_inr=10_000_000,
        budget_max_inr=15_000_000,
        localities=["Kharadi"],
    )

    from homeagent.scrapers.base import RawListing

    in_scope = RawListing(
        portal="nobroker",
        portal_listing_id="1",
        url="https://x",
        title="3 BHK",
        price_inr=12_000_000,
        bhk=3.0,
        locality="Kharadi",
    )
    out_of_locality = RawListing(
        portal="nobroker",
        portal_listing_id="2",
        url="https://x",
        title="3 BHK",
        price_inr=12_000_000,
        bhk=3.0,
        locality="Baner",
    )
    too_expensive = RawListing(
        portal="nobroker",
        portal_listing_id="3",
        url="https://x",
        title="3 BHK",
        price_inr=25_000_000,
        bhk=3.0,
        locality="Kharadi",
    )
    wrong_bhk = RawListing(
        portal="nobroker",
        portal_listing_id="4",
        url="https://x",
        title="1 BHK",
        price_inr=12_000_000,
        bhk=1.0,
        locality="Kharadi",
    )

    assert scraper._matches(in_scope, criteria)
    assert not scraper._matches(out_of_locality, criteria)
    assert not scraper._matches(too_expensive, criteria)
    assert not scraper._matches(wrong_bhk, criteria)


def test_nobroker_to_listing_db_roundtrip(tmp_db):
    from homeagent import db
    from homeagent.scrapers.base import RawListing
    from homeagent.scrapers.nobroker import to_listing

    raw = RawListing(
        portal="nobroker",
        portal_listing_id="xyz123",
        url="https://www.nobroker.in/property/sale/pune/Kharadi/detail/xyz123",
        title="3 BHK in Kharadi",
        price_inr=13_000_000,
        built_up_area_sqft=1200.0,
        bhk=3.0,
        locality="Kharadi",
        project_name="Skyline",
        builder="Acme",
    )
    listing = to_listing(raw)
    lid = db.upsert_listing(tmp_db, listing)
    fetched = db.get_listing(tmp_db, lid)
    assert fetched is not None
    assert fetched.title == "3 BHK in Kharadi"
    assert fetched.raw["project_name"] == "Skyline"
