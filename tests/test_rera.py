"""MahaRERA parser tests — fixture-backed, no network."""

from __future__ import annotations

from pathlib import Path

from homeagent.verification import rera


def test_parse_search_results(fixtures_dir: Path):
    html = (fixtures_dir / "rera_search.html").read_text()
    hits = rera.parse_search_results(html)

    assert len(hits) == 3
    ids = [h.rera_id for h in hits]
    assert "P52100012345" in ids
    assert "P51800022222" in ids

    skyline_heights = next(h for h in hits if h.rera_id == "P52100012345")
    assert skyline_heights.project_name == "Skyline Heights"
    assert skyline_heights.promoter == "Skyline Developers Pvt Ltd"
    assert skyline_heights.locality == "Pune"
    assert "P52100012345" in skyline_heights.detail_url


def test_parse_detail(fixtures_dir: Path):
    html = (fixtures_dir / "rera_detail.html").read_text()
    entry = rera.parse_detail(html, rera_id="P52100012345")

    assert entry.rera_id == "P52100012345"
    assert entry.project_name == "Skyline Heights"
    assert entry.promoter == "Skyline Developers Pvt Ltd"
    assert entry.status == "Registered"
    assert entry.completion_date == "2026-12-31"
    assert entry.complaints_count == 2


def test_best_match_prefers_exact_overlap():
    from homeagent.verification.rera import RERASearchHit, _best_match

    hits = [
        RERASearchHit("A", "Skyline Heights", None, None, "u/a"),
        RERASearchHit("B", "Skyline Greens", None, None, "u/b"),
        RERASearchHit("C", "Marvel Ribera Phase 2", None, None, "u/c"),
    ]
    best = _best_match(hits, "Skyline Heights")
    assert best is not None and best.rera_id == "A"


def test_best_match_returns_none_when_no_overlap():
    from homeagent.verification.rera import RERASearchHit, _best_match

    hits = [RERASearchHit("X", "Totally Different Project", None, None, "u")]
    assert _best_match(hits, "Skyline Heights") is None
