"""MahaRERA-as-discovery-source scraper.

Treats the public MahaRERA project registry as a "portal" (`portal="maharera"`)
that yields candidate listings — projects registered for sale in the user's
preferred localities. Unlike Magicbricks/99acres listings, RERA projects have
no price; the report tells the user to phone the builder for current rates.

**Why this exists** (vs just using `verification/rera.py`):
- `verification/rera.py` is *reactive*: given a project name from a portal,
  check that it's registered.
- This module is *proactive*: enumerate registered projects in your localities
  whether or not any portal happens to list them. That's the cleanest way to
  side-step portal contact-form telemarketing.

**v1.5 scope** (search-card only):
- We paginate the public search-results page (plain httpx, no auth) for each
  locality keyword.
- We filter on what the result card exposes: pincode + last-modified date.
- We do NOT fetch the per-project detail page — the new "beta public view"
  is an SPA that doesn't render content under headless Chrome and whose
  underlying APIs return 401. Possession date, BHK breakdown, and
  registered-vs-lapsed status are deferred to v2.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterator

from homeagent.scrapers.base import RawListing, SearchCriteria
from homeagent.verification.rera import RERASearchHit, paginated_search

log = logging.getLogger(__name__)

# Pincode → locality mapping for the localities the user cares about.
# Source: India Post records + manual cross-check of MahaRERA cards from
# this area. Each value is the set of pincodes officially covering that
# locality; many overlap (Kharadi/Chandan Nagar/Viman Nagar/Keshav Nagar
# all share 411014). Hadapsar's 411028 deliberately does NOT appear under
# any included locality — that's how we keep Hadapsar out without needing
# a per-card text match.
LOCALITY_PINCODES: dict[str, list[str]] = {
    "kharadi": ["411014"],
    "chandan nagar": ["411014"],
    "viman nagar": ["411014"],
    "vimannagar": ["411014"],
    "keshav nagar": ["411036", "411014"],
    "mundhwa": ["411036"],
    "wagholi": ["412207"],
    "manjari": ["412307", "412308"],
    "manjari khurd": ["412307"],
    "manjari budruk": ["412308"],
    # Excluded by default — listed only so localities_exclude resolves.
    "hadapsar": ["411028", "411013", "411060"],
}

# Reverse map: pincode → the canonical locality name we'd put on a Listing,
# so the downstream `locality_fit` check matches against `criteria.localities`
# rather than rejecting on the raw RERA taluka ("Haveli"). When a pincode
# covers multiple localities (411014 = Kharadi + Chandan Nagar + Viman Nagar),
# pick the most prominent name — the substring match in locality_fit will
# still accept the smaller localities since their pincode is the same.
PINCODE_TO_LOCALITY: dict[str, str] = {
    "411014": "Kharadi",
    "411036": "Mundhwa",
    "412207": "Wagholi",
    "412307": "Manjari Khurd",
    "412308": "Manjari Budruk",
}


class MahaReraScraper:
    name = "maharera"

    def search(self, criteria: SearchCriteria, limit: int = 10) -> Iterator[RawListing]:
        include_pins = _pincodes_for(criteria.localities)
        exclude_pins = _pincodes_for(criteria.localities_exclude)
        allowed_pincodes = include_pins - exclude_pins
        log.info(
            "maharera: searching by %d localities -> %d include pincodes (%s), %d exclude",
            len(criteria.localities), len(include_pins), sorted(include_pins), len(exclude_pins),
        )
        if not allowed_pincodes:
            log.warning("maharera: no pincodes resolved from localities; nothing to search")
            return

        cutoff = _cutoff_date(criteria.rera_max_last_modified_months)

        seen: dict[str, RERASearchHit] = {}
        for query in _search_keywords(criteria):
            log.info("maharera: paginated_search(%r)", query)
            for hit in paginated_search(query):
                if hit.rera_id not in seen:
                    seen[hit.rera_id] = hit

        log.info("maharera: %d unique candidates across keyword searches", len(seen))

        count = 0
        for hit in seen.values():
            if not _passes_filters(hit, allowed_pincodes, cutoff):
                continue
            yield _to_raw_listing(hit)
            count += 1
            if count >= limit:
                break

    # The Scraper protocol requires these even though we don't fetch detail pages.
    # They're no-ops so the existing graph code can treat this scraper uniformly.

    def fetch_detail(self, url: str) -> RawListing:  # pragma: no cover — not used in v1.5
        raise NotImplementedError("MahaRERA detail-page fetch is deferred to v2")

    def parse_search_results(self, html: str, base_url: str = "") -> list[RawListing]:
        from homeagent.verification.rera import parse_search_results as _parse
        return [_to_raw_listing(h) for h in _parse(html)]

    def parse_detail(self, html: str, url: str) -> RawListing:  # pragma: no cover
        raise NotImplementedError("MahaRERA detail-page parse is deferred to v2")


def _pincodes_for(localities: list[str]) -> set[str]:
    out: set[str] = set()
    for loc in localities:
        key = loc.strip().lower()
        pins = LOCALITY_PINCODES.get(key)
        if pins:
            out.update(pins)
        else:
            log.debug("maharera: no pincode mapping for locality %r — skipping", loc)
    return out


def _search_keywords(criteria: SearchCriteria) -> list[str]:
    """Build the search query list. Skip excluded localities and anything we
    don't have a pincode mapping for — those terms would just produce noise
    we'd reject downstream anyway."""
    excluded = {x.strip().lower() for x in criteria.localities_exclude}
    out: list[str] = []
    for loc in criteria.localities:
        key = loc.strip().lower()
        if key in excluded:
            continue
        if key not in LOCALITY_PINCODES:
            continue
        out.append(loc.strip())
    # Dedupe while preserving order, then collapse near-synonyms ("Viman Nagar"
    # / "Vimannagar") — MahaRERA's text search is loose enough that one query
    # covers both spellings.
    seen: set[str] = set()
    deduped: list[str] = []
    for q in out:
        k = q.lower().replace(" ", "")
        if k in seen:
            continue
        seen.add(k)
        deduped.append(q)
    return deduped


def _cutoff_date(months: int) -> date:
    today = date.today()
    # Approximate months → days; fine for a "stale beyond N months" cutoff.
    return date.fromordinal(today.toordinal() - months * 30)


def _passes_filters(hit: RERASearchHit, allowed_pincodes: set[str], cutoff: date) -> bool:
    if not hit.pincode or hit.pincode not in allowed_pincodes:
        return False
    if hit.last_modified:
        try:
            lm = datetime.fromisoformat(hit.last_modified).date()
        except ValueError:
            lm = None
        if lm and lm < cutoff:
            return False
    return True


def _to_raw_listing(hit: RERASearchHit) -> RawListing:
    """RERA hits have no price, no area, no BHK on the search card — we yield
    a sparse RawListing whose 'extra' carries the registry metadata so the
    report renderer can surface it as 'phone the builder' instead of a price
    ratio.

    `locality` is resolved from pincode (see `PINCODE_TO_LOCALITY`) rather
    than the RERA taluka — `locality_fit` substring-matches against
    `criteria.localities`, which lists user-recognized names like "Kharadi"
    that don't appear in the taluka field ("Haveli")."""
    locality = PINCODE_TO_LOCALITY.get(hit.pincode or "", hit.locality)
    return RawListing(
        portal="maharera",
        portal_listing_id=hit.rera_id,
        url=hit.detail_url or f"https://maharerait.maharashtra.gov.in/public/project/view/{hit.numeric_id or ''}",
        title=hit.project_name,
        price_inr=None,
        carpet_area_sqft=None,
        built_up_area_sqft=None,
        bhk=None,
        locality=locality,
        project_name=hit.project_name,
        builder=hit.promoter,
        status="new",  # registry projects are always for new construction
        extra={
            "rera_id": hit.rera_id,
            "pincode": hit.pincode,
            "district": hit.district,
            "taluka": hit.locality,  # preserve the raw taluka for transparency
            "last_modified": hit.last_modified,
            "numeric_id": hit.numeric_id,
            "source": "maharera",
        },
    )
