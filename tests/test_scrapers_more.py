"""Parse tests for the Magicbricks and 99acres scrapers."""

from __future__ import annotations

from pathlib import Path

from homeagent.scrapers.magicbricks import MagicbricksScraper
from homeagent.scrapers.ninetynineacres import NinetyNineAcresScraper


def test_magicbricks_search_parse(fixtures_dir: Path):
    html = (fixtures_dir / "magicbricks_search.html").read_text()
    scraper = MagicbricksScraper()
    results = scraper.parse_search_results(html)

    assert len(results) == 2
    wagholi = next(r for r in results if r.locality == "Wagholi")
    assert wagholi.bhk == 3.0
    assert wagholi.price_inr == 12_000_000
    assert wagholi.built_up_area_sqft == 1250.0
    assert wagholi.portal == "magicbricks"
    assert wagholi.portal_listing_id == "4d4233333437333739"

    kharadi = next(r for r in results if r.locality == "Kharadi")
    assert kharadi.bhk == 2.5
    assert kharadi.price_inr == 11_500_000


def test_99acres_search_parse(fixtures_dir: Path):
    html = (fixtures_dir / "99acres_search.html").read_text()
    scraper = NinetyNineAcresScraper()
    results = scraper.parse_search_results(html)

    assert len(results) == 2
    keshav = next(r for r in results if r.locality == "Keshav Nagar")
    assert keshav.bhk == 3.0
    assert keshav.price_inr == 13_500_000
    assert keshav.portal == "99acres"
    assert keshav.portal_listing_id == "A56789012"

    mundhwa = next(r for r in results if r.locality == "Mundhwa")
    assert mundhwa.bhk == 2.5
    assert mundhwa.price_inr == 10_800_000
