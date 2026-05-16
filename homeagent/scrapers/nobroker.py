"""NoBroker scraper.

NoBroker is React-rendered, so we use Playwright for live fetches. The parse functions are
pure (HTML in → RawListing out) so tests run offline against fixtures in `tests/fixtures/`.

Selector strategy: NoBroker uses CSS-module hashed class names (e.g. `nb__1abc2`) that change
between deploys. We prefer attribute-based and semantic selectors (`data-*` attrs, headings,
structured-data JSON-LD blocks) to stay robust.

If selectors break on a live run, save the HTML, drop it in `tests/fixtures/`, and update the
parsers against the new layout.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from selectolax.parser import HTMLParser

from homeagent.scrapers.base import RawListing, Scraper, SearchCriteria, fetch_html

log = logging.getLogger(__name__)

BASE = "https://www.nobroker.in"


def _parse_inr(text: str) -> int | None:
    """Parse Indian price strings like '₹1.25 Cr', '95 Lakh', '1,25,00,000'."""
    if not text:
        return None
    t = text.replace("₹", "").replace(",", "").strip().lower()
    m = re.match(r"^\s*([\d.]+)\s*(cr|crore|lakh|lac|l|k)?", t)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("cr", "crore"):
        return int(value * 10_000_000)
    if unit in ("lakh", "lac", "l"):
        return int(value * 100_000)
    if unit == "k":
        return int(value * 1_000)
    return int(value)


def _parse_area(text: str) -> float | None:
    """Pull a sqft number out of strings like '1,250 sq ft', '1100 sqft'."""
    if not text:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:sq\s*ft|sqft|sft)", text, re.I)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _parse_bhk(text: str) -> float | None:
    """Extract BHK from '2.5 BHK Apartment' / '3BHK'. Returns 2.5, 3.0 etc."""
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*bhk", text, re.I)
    return float(m.group(1)) if m else None


def _jsonld_blocks(tree: HTMLParser) -> list[dict]:
    blocks: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            blocks.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


class NoBrokerScraper:
    name = "nobroker"

    def build_search_url(self, criteria: SearchCriteria, page: int = 1) -> str:
        """Build a NoBroker search URL for the given criteria.

        NoBroker uses path-based filtering. For an MVP we hit the city-wide buy page and rely
        on locality filtering downstream — keeps the URL construction simple and robust.
        """
        city = criteria.city.lower()
        return f"{BASE}/property/sale/{city}/multiple?searchParam=&radius=2.0&page={page}"

    # --------------- Live fetch ---------------

    def search(self, criteria: SearchCriteria, limit: int = 10) -> Iterator[RawListing]:
        url = self.build_search_url(criteria)
        html = fetch_html(url)
        results = self.parse_search_results(html, base_url=BASE)
        log.info("nobroker: %d listings parsed from search page", len(results))

        # Filter by criteria (locality whitelist, BHK, price band)
        for raw in results[:limit]:
            if not self._matches(raw, criteria):
                continue
            # Enrich with detail-page fields if URL present
            try:
                detail = self.fetch_detail(raw.url) if raw.url else raw
                yield detail
            except Exception as e:
                log.warning("detail fetch failed for %s: %s", raw.url, e)
                yield raw

    def fetch_detail(self, url: str) -> RawListing:
        html = fetch_html(url)
        return self.parse_detail(html, url=url)

    # --------------- Pure parsers (testable offline) ---------------

    def parse_search_results(self, html: str, base_url: str = BASE) -> list[RawListing]:
        tree = HTMLParser(html)
        out: list[RawListing] = []

        # Strategy 1: JSON-LD ItemList (NoBroker historically embeds search results here)
        for block in _jsonld_blocks(tree):
            if block.get("@type") == "ItemList":
                for item in block.get("itemListElement", []) or []:
                    raw = self._raw_from_jsonld(item)
                    if raw:
                        out.append(raw)

        if out:
            return out

        # Strategy 2: cards with data-id / itemprop attributes
        for card in tree.css('[data-id], [itemprop="itemListElement"]'):
            listing_id = card.attributes.get("data-id") or card.css_first('meta[itemprop="position"]')
            if not listing_id:
                continue
            link = card.css_first("a[href]")
            href = link.attributes.get("href", "") if link else ""
            url = href if href.startswith("http") else f"{base_url}{href}"
            title_node = card.css_first('h2, h3, [itemprop="name"]')
            title = title_node.text(strip=True) if title_node else "(untitled)"
            price_node = card.css_first('[data-price], [itemprop="price"], .price')
            price = _parse_inr(price_node.text(strip=True)) if price_node else None
            area_node = card.css_first('[data-area], .area, [itemprop="floorSize"]')
            area = _parse_area(area_node.text(strip=True)) if area_node else None
            locality_node = card.css_first('[itemprop="addressLocality"], .locality')
            locality = locality_node.text(strip=True) if locality_node else None

            out.append(
                RawListing(
                    portal="nobroker",
                    portal_listing_id=str(listing_id) if isinstance(listing_id, str) else listing_id.text(strip=True),
                    url=url,
                    title=title,
                    price_inr=price,
                    built_up_area_sqft=area,
                    bhk=_parse_bhk(title),
                    locality=locality,
                )
            )
        return out

    def parse_detail(self, html: str, url: str) -> RawListing:
        tree = HTMLParser(html)

        # Try JSON-LD first — most reliable
        for block in _jsonld_blocks(tree):
            if block.get("@type") in {"Product", "Residence", "ApartmentComplex", "Offer"}:
                raw = self._raw_from_jsonld({"item": block, "url": url})
                if raw:
                    return raw

        # Fallback: scrape visible fields
        title_node = tree.css_first("h1, h2")
        title = title_node.text(strip=True) if title_node else "(untitled)"

        price_node = tree.css_first('[itemprop="price"], [data-price], .price, h2:contains("₹")')
        price = _parse_inr(price_node.text(strip=True)) if price_node else _parse_inr(title)

        area_node = tree.css_first('[itemprop="floorSize"], [data-area]')
        area_text = area_node.text(strip=True) if area_node else ""
        if not area_text:
            # Look for any element mentioning sqft
            for n in tree.css("*"):
                t = n.text(strip=True)
                if "sqft" in t.lower() or "sq ft" in t.lower():
                    area_text = t
                    break
        area = _parse_area(area_text)

        locality_node = tree.css_first('[itemprop="addressLocality"], .locality')
        locality = locality_node.text(strip=True) if locality_node else None

        builder_node = tree.css_first('[data-builder], .builder-name')
        builder = builder_node.text(strip=True) if builder_node else None

        project_node = tree.css_first('[data-project], .project-name')
        project = project_node.text(strip=True) if project_node else None

        # NoBroker URLs include a property id like /property/.../detail/8a808fdf...
        m = re.search(r"/detail/([a-z0-9]+)", url)
        listing_id = m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]

        return RawListing(
            portal="nobroker",
            portal_listing_id=listing_id,
            url=url,
            title=title,
            price_inr=price,
            built_up_area_sqft=area,
            bhk=_parse_bhk(title),
            locality=locality,
            project_name=project,
            builder=builder,
        )

    # --------------- Helpers ---------------

    def _raw_from_jsonld(self, item: dict) -> RawListing | None:
        """Coerce a JSON-LD ItemList entry into a RawListing."""
        inner = item.get("item") if isinstance(item.get("item"), dict) else item
        url = inner.get("url") or item.get("url")
        if not url:
            return None
        name = inner.get("name", "(untitled)")
        offer = inner.get("offers") or {}
        price_raw = offer.get("price") if isinstance(offer, dict) else None
        try:
            price = int(float(price_raw)) if price_raw is not None else None
        except (TypeError, ValueError):
            price = None
        floor_size = inner.get("floorSize") or {}
        if isinstance(floor_size, dict):
            area = floor_size.get("value")
            try:
                area = float(area) if area is not None else None
            except (TypeError, ValueError):
                area = None
        else:
            area = None

        address = inner.get("address") or {}
        locality = address.get("addressLocality") if isinstance(address, dict) else None

        m = re.search(r"/detail/([a-z0-9]+)", url)
        listing_id = m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]

        return RawListing(
            portal="nobroker",
            portal_listing_id=listing_id,
            url=url,
            title=name,
            price_inr=price,
            built_up_area_sqft=area,
            bhk=_parse_bhk(name),
            locality=locality,
        )

    def _matches(self, raw: RawListing, c: SearchCriteria) -> bool:
        if raw.bhk and c.bhk and raw.bhk not in c.bhk:
            return False
        if raw.price_inr:
            if raw.price_inr < c.budget_min_inr * 0.85:
                return False
            if raw.price_inr > c.budget_max_inr * 1.15:
                return False
        if c.localities and raw.locality:
            wanted = [l.lower() for l in c.localities]
            if not any(w in raw.locality.lower() for w in wanted):
                return False
        return True


def to_listing(raw: RawListing):
    """Convert a RawListing into the DB model. Imported here to keep models out of scraper."""
    from homeagent.models import Listing

    return Listing(
        portal=raw.portal,
        portal_listing_id=raw.portal_listing_id,
        url=raw.url,
        title=raw.title,
        price_inr=raw.price_inr,
        carpet_area_sqft=raw.carpet_area_sqft,
        built_up_area_sqft=raw.built_up_area_sqft,
        bhk=raw.bhk,
        locality=raw.locality,
        status=raw.status if raw.status in {"new", "resale", "unknown"} else "unknown",
        raw={
            "project_name": raw.project_name,
            "builder": raw.builder,
            **raw.extra,
        },
    )
