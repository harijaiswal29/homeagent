"""Tests for the MahaRERA discovery scraper (search-card-only, v1.5).

Network-free: everything runs against the captured search fixture in
`tests/fixtures/maharera_search.html`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from homeagent.scrapers.base import SearchCriteria
from homeagent.scrapers.maharera import (
    MahaReraScraper,
    _cutoff_date,
    _passes_filters,
    _pincodes_for,
    _search_keywords,
    _to_raw_listing,
)
from homeagent.verification.rera import RERASearchHit, parse_search_results


def test_parser_captures_new_fields(fixtures_dir: Path):
    """Search-card parsing should now also expose pincode, district, last_modified, numeric_id."""
    html = (fixtures_dir / "maharera_search.html").read_text()
    hits = parse_search_results(html)
    first = hits[0]
    assert first.pincode == "440018"
    assert first.district == "Nagpur"
    assert first.last_modified == "2017-08-09"
    assert first.numeric_id == "931"


def test_pincodes_for_resolves_known_localities():
    pins = _pincodes_for(["Kharadi", "Wagholi", "Mundhwa", "Manjari"])
    # Kharadi=411014, Wagholi=412207, Mundhwa=411036, Manjari=412307+412308
    assert "411014" in pins
    assert "412207" in pins
    assert "411036" in pins
    assert "412307" in pins
    assert "412308" in pins
    # Hadapsar (411028) must NOT leak in via any of the included localities
    assert "411028" not in pins


def test_pincodes_for_skips_unknown_localities():
    pins = _pincodes_for(["Made Up Place", "Kharadi"])
    assert pins == {"411014"}


def test_search_keywords_dedupes_and_excludes():
    c = SearchCriteria(
        city="Pune", bhk=[2.5, 3.0], budget_min_inr=10_000_000, budget_max_inr=15_000_000,
        localities=["Kharadi", "Viman Nagar", "Vimannagar", "Hadapsar", "Wagholi"],
        localities_exclude=["Hadapsar"],
    )
    keywords = _search_keywords(c)
    # Hadapsar excluded; "Viman Nagar"/"Vimannagar" collapsed to one query
    assert "Hadapsar" not in keywords
    assert sum(1 for k in keywords if k.lower().replace(" ", "") == "vimannagar") == 1
    assert "Kharadi" in keywords
    assert "Wagholi" in keywords


def test_passes_filters_accepts_recent_kharadi():
    today_iso = date.today().isoformat()
    hit = RERASearchHit(
        rera_id="P52100099999", project_name="Test", promoter="Test Pvt",
        locality="Haveli", detail_url=None, pincode="411014", last_modified=today_iso,
    )
    cutoff = _cutoff_date(36)
    assert _passes_filters(hit, allowed_pincodes={"411014", "412207"}, cutoff=cutoff)


def test_passes_filters_rejects_wrong_pincode():
    """Hadapsar pincode 411028 must be rejected even if the project name matches a keyword."""
    hit = RERASearchHit(
        rera_id="P5", project_name="Hadapsar Heights", promoter="X",
        locality="Haveli", detail_url=None, pincode="411028", last_modified="2026-01-01",
    )
    assert not _passes_filters(hit, allowed_pincodes={"411014", "412207"}, cutoff=_cutoff_date(36))


def test_passes_filters_rejects_missing_pincode():
    hit = RERASearchHit(
        rera_id="P5", project_name="X", promoter="X",
        locality="Haveli", detail_url=None, pincode=None, last_modified="2026-01-01",
    )
    assert not _passes_filters(hit, allowed_pincodes={"411014"}, cutoff=_cutoff_date(36))


def test_passes_filters_rejects_stale_last_modified():
    """A 2017-modified project should fail the 36-month freshness gate today."""
    hit = RERASearchHit(
        rera_id="P5", project_name="X", promoter="X",
        locality="Haveli", detail_url=None, pincode="411014", last_modified="2017-08-09",
    )
    assert not _passes_filters(hit, allowed_pincodes={"411014"}, cutoff=_cutoff_date(36))


def test_passes_filters_accepts_when_last_modified_unparseable():
    """Garbage date string shouldn't crash or silently drop the candidate."""
    hit = RERASearchHit(
        rera_id="P5", project_name="X", promoter="X",
        locality="Haveli", detail_url=None, pincode="411014", last_modified="not-a-date",
    )
    assert _passes_filters(hit, allowed_pincodes={"411014"}, cutoff=_cutoff_date(36))


def test_to_raw_listing_shape():
    hit = RERASearchHit(
        rera_id="P52100099999", project_name="Test Project", promoter="Test Builder Pvt Ltd",
        locality="Haveli", detail_url="https://x/public/project/view/5022",
        pincode="411014", district="Pune", last_modified="2026-03-01", numeric_id="5022",
    )
    raw = _to_raw_listing(hit)
    assert raw.portal == "maharera"
    assert raw.portal_listing_id == "P52100099999"
    assert raw.title == "Test Project"
    assert raw.builder == "Test Builder Pvt Ltd"
    assert raw.price_inr is None  # explicit: RERA has no prices
    assert raw.bhk is None
    assert raw.status == "new"
    # Pincode 411014 resolves to "Kharadi" so locality_fit matches downstream;
    # the raw taluka is preserved in extra for transparency.
    assert raw.locality == "Kharadi"
    assert raw.extra["taluka"] == "Haveli"
    assert raw.extra["pincode"] == "411014"
    assert raw.extra["last_modified"] == "2026-03-01"
    assert raw.extra["numeric_id"] == "5022"
    assert raw.extra["rera_id"] == "P52100099999"


def test_to_raw_listing_falls_back_to_taluka_when_pincode_unmapped():
    """If the pincode isn't in the reverse map, keep the raw taluka so we don't drop the field."""
    hit = RERASearchHit(
        rera_id="P5", project_name="X", promoter="Y", locality="Some Taluka",
        detail_url=None, pincode="999999",
    )
    raw = _to_raw_listing(hit)
    assert raw.locality == "Some Taluka"


def test_scraper_parse_search_results_yields_raw_listings(fixtures_dir: Path):
    """The Scraper-protocol parse_search_results() shim adapts hits → RawListings."""
    scraper = MahaReraScraper()
    html = (fixtures_dir / "maharera_search.html").read_text()
    raws = scraper.parse_search_results(html)
    assert len(raws) == 10
    assert all(r.portal == "maharera" for r in raws)
    assert all(r.price_inr is None for r in raws)
    # First card from the fixture is the Nagpur Godrej project — should map cleanly
    first = raws[0]
    assert first.portal_listing_id == "P50500004427"
    assert first.title == "Godrej Anandam"
    assert first.builder == "Godrej Properties Limited"
