"""MahaRERA parser tests — fixture-backed, no network."""

from __future__ import annotations

from pathlib import Path

from homeagent.verification import rera


def test_parse_search_results(fixtures_dir: Path):
    """Live-captured Godrej search page should yield 10 result cards with clean fields."""
    html = (fixtures_dir / "maharera_search.html").read_text()
    hits = rera.parse_search_results(html)

    assert len(hits) == 10
    # Every hit must have a parsed RERA ID and project name
    assert all(h.rera_id.startswith("P") for h in hits)
    assert all(h.project_name and h.project_name != "(unknown)" for h in hits)

    # Spot-check the first card we know is "Godrej Anandam" (P50500004427, Nagpur (Urban))
    first = hits[0]
    assert first.rera_id == "P50500004427"
    assert first.project_name == "Godrej Anandam"
    assert first.promoter == "Godrej Properties Limited"
    assert first.locality and "Nagpur" in first.locality
    assert first.detail_url and "public/project/view/" in first.detail_url


def test_parse_search_results_handles_new_id_format(fixtures_dir: Path):
    """Regression: MahaRERA now also issues ``PR{13 digits}`` / ``PM{13 digits}`` IDs.
    The old regex ``P\\d{2,5}[A-Z\\d]{4,12}`` missed those entirely.

    Page 11 of the Godrej query contains Godrej Ivara under ID PR1260002502426.
    """
    html = (fixtures_dir / "maharera_search_godrej_p11.html").read_text()
    hits = rera.parse_search_results(html)

    ids = {h.rera_id for h in hits}
    assert "PR1260002502426" in ids
    ivara = next(h for h in hits if h.rera_id == "PR1260002502426")
    assert ivara.project_name == "Godrej Ivara"


def test_parse_total_pages(fixtures_dir: Path):
    """Pagination footer reports 11 total pages for the Godrej query."""
    html = (fixtures_dir / "maharera_search.html").read_text()
    assert rera.parse_total_pages(html) == 11


def test_build_search_url_paginates():
    from homeagent.verification.rera import _build_search_url

    u1 = _build_search_url("Godrej", 1)
    u3 = _build_search_url("Godrej", 3)
    assert "project_name=Godrej" in u1
    assert "page=1" in u1 and "page=3" in u3
    assert "project_state=27" in u1


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


def test_best_match_rejects_builder_only_overlap():
    """Regression: 'Godrej Ivara' must NOT match 'Godrej Anandam' just because both share
    the builder word. Same logic applies to 'Lodha Giardino' vs 'Lodha Fiorenza' etc."""
    from homeagent.verification.rera import RERASearchHit, _best_match

    hits = [
        RERASearchHit("A", "Godrej Anandam", None, None, "u/a"),
        RERASearchHit("B", "Godrej Properties Greens", None, None, "u/b"),
    ]
    assert _best_match(hits, "Godrej Ivara") is None


def test_best_match_accepts_phase_suffix():
    """MahaRERA registers each phase separately ('Pristine Allure PART2'); a two-word
    query should still match when the RERA name adds a phase/part suffix."""
    from homeagent.verification.rera import RERASearchHit, _best_match

    hits = [
        RERASearchHit("A", "Pristine Allure PART2", None, None, "u/a"),
        RERASearchHit("B", "Pristine Studio", None, None, "u/b"),
    ]
    best = _best_match(hits, "Pristine Allure")
    assert best is not None and best.rera_id == "A"
