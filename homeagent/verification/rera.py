"""MahaRERA project lookup.

MahaRERA (`https://maharera.maharashtra.gov.in/`) has no public API. After clicking
"Search" in the project search form once, the canonical URL for results is a plain GET:

    /projects-search-result?project_name={name}&project_state=27&page={N}&op=Search

— and that URL works standalone (no session cookie required). We hit it with httpx and
paginate up to ``_MAX_PAGES`` pages, since common builder names ("Godrej", "Lodha") can
have 100+ registered projects and the target may sit on the last page.

Each result card carries enough metadata for our `rera_match` check (rera_id,
project_name, promoter, district), so we don't fetch a per-project detail page; every hit
seen is cached in the `rera_entries` table keyed by rera_id, and a name index serves
offline re-runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser

from homeagent.db import connect, get_rera, upsert_rera
from homeagent.models import RERAEntry
from homeagent.scrapers.base import USER_AGENTS, load_cached, polite_sleep, save_cache

log = logging.getLogger(__name__)

SEARCH_URL = "https://maharera.maharashtra.gov.in/projects-search-result"
MAHARASHTRA_STATE_CODE = "27"

# Cap on pages to traverse for a single query. A common builder name can return 100+
# results across 10+ pages; we trade some recall for not hammering the site (polite_sleep
# already runs between fetches).
_MAX_PAGES = 15

# Jaccard threshold on lowercased word-set overlap between query and a candidate project
# name. 0.5 means: for a two-word query like "Godrej Ivara", a candidate must share both
# words (intersection 2 / union 2 = 1.0) or share one word plus add at most one
# (intersection 1 / union 3 = 0.33 → rejected). Lower values produced false positives
# like "Godrej Ivara" → "Godrej Anandam".
_NAME_MATCH_THRESHOLD = 0.5

# MahaRERA registration IDs come in two shapes:
#   Old: ``P<11 digits>``                    e.g. P50500004427
#   New: ``P[A-Z]<13 digits>``               e.g. PR1260002502426, PM1271012502176
# Accept both.
_RERA_ID_RE = re.compile(r"P[A-Z]?\d{6,18}")
_RERA_ID_PREFIXED_RE = re.compile(r"#\s*(" + _RERA_ID_RE.pattern + ")")


@dataclass
class RERASearchHit:
    rera_id: str
    project_name: str
    promoter: str | None
    locality: str | None
    detail_url: str | None


# --------------- Pure parsers ---------------


def parse_search_results(html: str) -> list[RERASearchHit]:
    """Extract project hits from a MahaRERA results page.

    Each result is a card: ``<div class="row shadow ... bg-body rounded">`` containing
    the RERA ID (in a leading ``# Pxxx`` paragraph), the project name (``h4.title4 >
    strong``), promoter (``p.darkBlue.bold``), district (first ``ul.listingList li a``),
    and a "View Details" link to ``maharerait.maharashtra.gov.in/public/project/view/{id}``.
    """
    tree = HTMLParser(html)
    hits: list[RERASearchHit] = []

    for card in tree.css("div.row.shadow.bg-body"):
        rera_id: str | None = None
        for p in card.css("p"):
            m = _RERA_ID_PREFIXED_RE.match(p.text(strip=True))
            if m:
                rera_id = m.group(1)
                break
        if not rera_id:
            continue

        name_node = (
            card.css_first("h4.title4 strong")
            or card.css_first("h4 strong")
            or card.css_first("h4")
        )
        project_name = name_node.text(strip=True) if name_node else "(unknown)"

        promoter_node = card.css_first("p.darkBlue.bold") or card.css_first("p.darkBlue")
        promoter = promoter_node.text(strip=True) if promoter_node else None

        locality: str | None = None
        district_link = card.css_first("ul.listingList li a")
        if district_link:
            locality = district_link.text(strip=True) or None

        detail_url: str | None = None
        view = card.css_first('a[href*="public/project/view/"]')
        if view:
            detail_url = view.attributes.get("href")

        hits.append(
            RERASearchHit(
                rera_id=rera_id,
                project_name=project_name,
                promoter=promoter,
                locality=locality,
                detail_url=detail_url,
            )
        )
    return hits


def parse_total_pages(html: str) -> int:
    """Extract the total page count from the pagination footer.

    MahaRERA renders ``<span class="pagesCount" data-total="109" data-current-data="11">``
    where ``data-current-data`` is actually the total page count (Drupal naming).
    """
    tree = HTMLParser(html)
    node = tree.css_first("span.pagesCount")
    if not node:
        return 1
    raw = node.attributes.get("data-current-data") or node.text(strip=True) or "1"
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


# --------------- Live lookup ---------------


def _build_search_url(project_name: str, page: int) -> str:
    return (
        f"{SEARCH_URL}?project_name={project_name.replace(' ', '+')}"
        f"&project_state={MAHARASHTRA_STATE_CODE}&page={page}&op=Search"
    )


def _fetch_page(project_name: str, page: int, *, use_cache: bool = True) -> str | None:
    url = _build_search_url(project_name, page)
    if use_cache:
        cached = load_cached(url)
        if cached:
            log.info("RERA cache hit: %r page %d", project_name, page)
            return cached

    log.info("RERA fetching: %r page %d", project_name, page)
    try:
        headers = {
            "User-Agent": USER_AGENTS[0],
            "Accept": "text/html,application/xhtml+xml",
        }
        r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log.debug("RERA fetch failed for %r p%d: %s", project_name, page, e)
        return None

    save_cache(url, html)
    polite_sleep()
    return html


def _paginated_search(project_name: str, *, use_cache: bool = True) -> list[RERASearchHit]:
    """Walk pages until we run out, or hit _MAX_PAGES, or a page yields no hits."""
    all_hits: list[RERASearchHit] = []
    total_pages: int | None = None

    for page in range(1, _MAX_PAGES + 1):
        html = _fetch_page(project_name, page, use_cache=use_cache)
        if html is None:
            break

        if total_pages is None:
            total_pages = parse_total_pages(html)
            log.info("RERA %r has %d total pages", project_name, total_pages)

        hits = parse_search_results(html)
        if not hits:
            break  # empty page → we've passed the last result page
        all_hits.extend(hits)

        if page >= total_pages:
            break

    return all_hits


def lookup_project(project_name: str, *, use_cache: bool = True) -> RERAEntry | None:
    """Look up a project on MahaRERA by name. Returns the closest match, or None.

    Lookup order:
      1. DB cache by project name (instant; offline-friendly after one warm run)
      2. Paginated httpx GETs → parse → best match → cache every hit
    """
    if not project_name or len(project_name) < 3:
        return None

    if use_cache:
        cached = _cache_lookup_by_name(project_name)
        if cached:
            log.info("RERA cache hit for %r → %s", project_name, cached.rera_id)
            return cached

    # MahaRERA's text search is prefix-leaning and trips on long multi-word queries:
    # "Godrej Ivara" returns 0; "Godrej" returns 109 across 11 pages. Try the full name
    # first, then fall back to the first word and rely on _best_match to filter.
    hits = _paginated_search(project_name, use_cache=use_cache)
    if not hits and " " in project_name:
        first_word = project_name.split()[0]
        if len(first_word) >= 3:
            hits = _paginated_search(first_word, use_cache=use_cache)

    if not hits:
        log.info("RERA: no hits for %r", project_name)
        return None

    # Cache every hit we saw — keeps the DB warm for adjacent queries on the same builder.
    with connect() as conn:
        for h in hits:
            if not get_rera(conn, h.rera_id):
                upsert_rera(
                    conn,
                    RERAEntry(
                        rera_id=h.rera_id,
                        project_name=h.project_name,
                        promoter=h.promoter,
                        status="Registered",
                        completion_date=None,
                        complaints_count=None,
                        raw_html=None,
                    ),
                )

    best = _best_match(hits, project_name)
    if best is None:
        return None

    with connect() as conn:
        return get_rera(conn, best.rera_id)


def _cache_lookup_by_name(project_name: str) -> RERAEntry | None:
    """Return a cached RERA entry whose project_name has high overlap with the query."""
    q = project_name.lower().strip()
    with connect() as conn:
        rows = conn.execute(
            "SELECT rera_id FROM rera_entries WHERE LOWER(project_name) LIKE ?",
            (f"%{q.split()[0]}%",),
        ).fetchall()
        candidates: list[RERAEntry] = []
        for row in rows:
            e = get_rera(conn, row["rera_id"])
            if e:
                candidates.append(e)
    if not candidates:
        return None
    scored = [(_overlap(c.project_name.lower(), q), c) for c in candidates]
    scored.sort(key=lambda x: -x[0])
    top_score, top = scored[0]
    return top if top_score >= _NAME_MATCH_THRESHOLD else None


def _best_match(hits: list[RERASearchHit], query: str) -> RERASearchHit | None:
    """Pick the search hit whose project_name has the highest word overlap with the query."""
    q = query.lower().strip()
    scored = [(_overlap(h.project_name.lower(), q), h) for h in hits]
    scored.sort(key=lambda x: -x[0])
    top_score, top = scored[0]
    return top if top_score >= _NAME_MATCH_THRESHOLD else None


def _overlap(a: str, b: str) -> float:
    """Crude Jaccard over word sets."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
