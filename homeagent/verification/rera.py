"""MahaRERA project lookup.

MahaRERA (`https://maharera.maharashtra.gov.in/`) has no public API. We search by project name
on the project search page, then fetch the registration detail page. Results are cached per
`rera_id` in the `rera_entries` table so we don't re-scrape.

This module separates HTTP fetch from HTML parse — parsers are pure and tested offline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from homeagent.db import connect, get_rera, upsert_rera
from homeagent.models import RERAEntry
from homeagent.scrapers.base import fetch_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://maharera.maharashtra.gov.in/projects/search-project?project_name={q}"
DETAIL_URL = "https://maharera.maharashtra.gov.in/project-details?id={rera_id}"


@dataclass
class RERASearchHit:
    rera_id: str
    project_name: str
    promoter: str | None
    locality: str | None
    detail_url: str


# --------------- Pure parsers ---------------


def parse_search_results(html: str) -> list[RERASearchHit]:
    """Extract project hits from the MahaRERA search results page."""
    tree = HTMLParser(html)
    hits: list[RERASearchHit] = []

    # MahaRERA's results are rendered as a table.
    for row in tree.css("table tbody tr"):
        cells = row.css("td")
        if len(cells) < 4:
            continue
        # Common column order: project name | promoter | district | RERA ID
        project = cells[0].text(strip=True)
        promoter = cells[1].text(strip=True)
        locality = cells[2].text(strip=True)
        rera_id = cells[3].text(strip=True)

        link = row.css_first("a[href]")
        detail_href = link.attributes.get("href", "") if link else ""
        if detail_href and not detail_href.startswith("http"):
            detail_href = f"https://maharera.maharashtra.gov.in{detail_href}"
        if not detail_href and rera_id:
            detail_href = DETAIL_URL.format(rera_id=rera_id)

        if rera_id:
            hits.append(
                RERASearchHit(
                    rera_id=rera_id,
                    project_name=project,
                    promoter=promoter or None,
                    locality=locality or None,
                    detail_url=detail_href,
                )
            )
    return hits


def parse_detail(html: str, rera_id: str) -> RERAEntry:
    """Parse the MahaRERA project detail page into a RERAEntry."""
    tree = HTMLParser(html)

    def find_value(label_pattern: str) -> str | None:
        """Find a label-value pair: looks for a cell/dt matching the regex, returns next sibling/dd."""
        for node in tree.css("th, dt, label, td, strong, b"):
            text = node.text(strip=True)
            if re.search(label_pattern, text, re.I):
                # Try next-sibling td/dd
                parent = node.parent
                if parent is None:
                    continue
                # Common pattern in MahaRERA: <th>Label</th><td>Value</td>
                siblings = list(parent.iter())
                try:
                    idx = siblings.index(node)
                    for sib in siblings[idx + 1:]:
                        v = sib.text(strip=True)
                        if v:
                            return v
                except (ValueError, AttributeError):
                    pass
        return None

    project_name = find_value(r"^project\s*name") or "(unknown)"
    promoter = find_value(r"promoter|developer")
    status = find_value(r"^status|registration\s*status")
    completion = find_value(r"completion|proposed\s*date")
    complaints_txt = find_value(r"complaints?")
    complaints: int | None = None
    if complaints_txt:
        m = re.search(r"\d+", complaints_txt)
        if m:
            complaints = int(m.group(0))

    return RERAEntry(
        rera_id=rera_id,
        project_name=project_name,
        promoter=promoter,
        status=status,
        completion_date=completion,
        complaints_count=complaints,
        raw_html=html[:50_000],  # truncate to keep DB sane
    )


# --------------- Live lookup (cached via DB) ---------------


def lookup_project(project_name: str, *, use_cache: bool = True) -> RERAEntry | None:
    """Look up a project on MahaRERA by name. Returns the first reasonable match, or None.

    Lookup order:
      1. DB cache by project name (cheap; supports offline runs after one warm fetch)
      2. Live search → detail fetch → cache
    """
    if not project_name or len(project_name) < 3:
        return None

    if use_cache:
        cached = _cache_lookup_by_name(project_name)
        if cached:
            log.info("RERA cache hit for %r → %s", project_name, cached.rera_id)
            return cached

    # Search for candidates
    search_url = SEARCH_URL.format(q=project_name.replace(" ", "+"))
    try:
        html = fetch_html(search_url, use_cache=use_cache)
    except Exception as e:
        # Silence noisy Playwright stack traces during offline runs; one debug line is enough.
        log.debug("RERA search failed for %r: %s", project_name, e)
        return None

    hits = parse_search_results(html)
    if not hits:
        log.info("RERA: no hits for %r", project_name)
        return None

    best = _best_match(hits, project_name)
    if best is None:
        return None

    # Cache hit?
    with connect() as conn:
        cached = get_rera(conn, best.rera_id)
        if cached:
            return cached

        try:
            detail_html = fetch_html(best.detail_url, use_cache=use_cache)
        except Exception as e:
            log.debug("RERA detail fetch failed for %s: %s", best.rera_id, e)
            return None

        entry = parse_detail(detail_html, rera_id=best.rera_id)
        # Use search-row metadata where detail page didn't yield it
        if not entry.promoter and best.promoter:
            entry.promoter = best.promoter
        upsert_rera(conn, entry)
        return entry


def _cache_lookup_by_name(project_name: str) -> RERAEntry | None:
    """Return a cached RERA entry whose project_name has high overlap with the query."""
    q = project_name.lower().strip()
    with connect() as conn:
        rows = conn.execute(
            "SELECT rera_id FROM rera_entries WHERE LOWER(project_name) LIKE ?",
            (f"%{q.split()[0]}%",),  # broad pre-filter on first word
        ).fetchall()
        candidates: list[RERAEntry] = []
        for row in rows:
            e = get_rera(conn, row["rera_id"])
            if e:
                candidates.append(e)
    if not candidates:
        return None
    # Pick best by word overlap
    scored = [(_overlap(c.project_name.lower(), q), c) for c in candidates]
    scored.sort(key=lambda x: -x[0])
    top_score, top = scored[0]
    return top if top_score > 0.3 else None


def _best_match(hits: list[RERASearchHit], query: str) -> RERASearchHit | None:
    """Pick the search hit whose project_name has the highest substring overlap with the query."""
    q = query.lower().strip()
    scored = [(_overlap(h.project_name.lower(), q), h) for h in hits]
    scored.sort(key=lambda x: -x[0])
    top_score, top = scored[0]
    return top if top_score > 0.3 else None


def _overlap(a: str, b: str) -> float:
    """Crude Jaccard over word sets."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
