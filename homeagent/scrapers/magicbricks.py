"""Magicbricks scraper.

Same structure as NoBroker: pure parsers for offline testing, Playwright-driven live fetch.
Magicbricks embeds property data in `<script type="application/ld+json">` Product blocks; we
target those for robustness against CSS-class churn.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from selectolax.parser import HTMLParser

from homeagent.scrapers.base import RawListing, SearchCriteria, fetch_html
from homeagent.scrapers.nobroker import _parse_area, _parse_bhk, _parse_inr

log = logging.getLogger(__name__)

BASE = "https://www.magicbricks.com"


def _area_from_url(url: str) -> float | None:
    """Magicbricks URLs embed the area: `.../3-BHK-1140-Sq-ft-Multistorey-Apartment-...`.

    Used as a fallback when the JSON-LD `floorSize` is absent.
    """
    m = re.search(r"(\d+(?:\.\d+)?)-Sq-ft", url, re.I)
    return float(m.group(1)) if m else None


def _jsonld_products(tree: HTMLParser) -> list[dict]:
    out: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if not isinstance(d, dict):
                continue
            if d.get("@type") in {"Product", "Residence", "Apartment", "ItemList"}:
                out.append(d)
    return out


class MagicbricksScraper:
    name = "magicbricks"

    def build_search_url(self, criteria: SearchCriteria, page: int = 1) -> str:
        city = criteria.city.replace(" ", "-")
        bhk_param = "&".join(f"bedroom={int(b) if b == int(b) else b}" for b in criteria.bhk)
        return (
            f"{BASE}/property-for-sale/residential-real-estate"
            f"?bedroom={','.join(str(int(b) if b == int(b) else b) for b in criteria.bhk)}"
            f"&proptype=Multistorey-Apartment,Builder-Floor-Apartment"
            f"&cityName={city}&BudgetMin={criteria.budget_min_inr}"
            f"&BudgetMax={criteria.budget_max_inr}&page={page}"
        )

    def search(self, criteria: SearchCriteria, limit: int = 10) -> Iterator[RawListing]:
        url = self.build_search_url(criteria)
        html = fetch_html(url)
        results = self.parse_search_results(html)
        log.info("magicbricks: %d listings parsed", len(results))
        count = 0
        for raw in results:
            if not self._matches(raw, criteria):
                continue
            yield raw
            count += 1
            if count >= limit:
                break

    def fetch_detail(self, url: str) -> RawListing:
        html = fetch_html(url)
        return self.parse_detail(html, url=url)

    def parse_search_results(self, html: str) -> list[RawListing]:
        """Parse search results.

        Magicbricks emits two parallel JSON-LD shapes per result page: a single ItemList of
        url+name pairs, and one Apartment block per listing carrying address.addressLocality
        plus geo. We harvest both and dedupe by URL, preferring the Apartment-derived row since
        it has the locality we need for filtering.

        Price and project_name aren't in JSON-LD — those live in the rendered card DOM
        (.mb-srp__card__price--amount and .mb-srp__card__developer--name--highlight). We walk
        the cards as a second pass and merge by pdpid.
        """
        tree = HTMLParser(html)
        by_url: dict[str, RawListing] = {}

        for block in _jsonld_products(tree):
            btype = block.get("@type")
            if btype == "ItemList":
                for item in block.get("itemListElement", []) or []:
                    inner = item.get("item") if isinstance(item.get("item"), dict) else item
                    raw = self._from_jsonld(inner)
                    if raw and raw.url not in by_url:
                        by_url[raw.url] = raw
            elif btype in {"Product", "Residence", "Apartment"}:
                raw = self._from_jsonld(block)
                if raw:
                    # Apartment blocks are richer (have locality); always prefer them.
                    by_url[raw.url] = raw

        # Second pass: merge price / project_name / area from the rendered card DOM.
        self._merge_card_dom(tree, by_url)
        return list(by_url.values())

    def _merge_card_dom(self, tree: HTMLParser, by_url: dict[str, RawListing]) -> None:
        """Enrich JSON-LD-derived listings with price + project name from the visible cards.

        Cards are matched to listings by the numeric MB id. Cards expose it on a wrapper
        element as ``id="propertiesAction{NNNN}"``; the JSON-LD URL embeds it hex-encoded
        as ``&id={hex("MB" + NNNN)}``.
        """
        # Build url → MB-id index by decoding the JSON-LD url's hex blob
        by_mbid: dict[str, RawListing] = {}
        for url, raw in by_url.items():
            m = re.search(r"[?&]id=([a-f0-9]+)", url)
            if not m:
                continue
            try:
                decoded = bytes.fromhex(m.group(1)).decode("ascii")
            except (ValueError, UnicodeDecodeError):
                continue
            if decoded.startswith("MB"):
                by_mbid[decoded[2:]] = raw

        for card in tree.css("div.mb-srp__card"):
            wrapper = card.css_first('[id^="propertiesAction"]')
            if not wrapper:
                continue
            mbid = wrapper.attributes.get("id", "").removeprefix("propertiesAction")
            raw = by_mbid.get(mbid)
            if raw is None:
                continue

            if raw.price_inr is None:
                price_node = card.css_first(".mb-srp__card__price--amount")
                if price_node:
                    raw.price_inr = _parse_inr(price_node.text(strip=True))

            if raw.built_up_area_sqft is None:
                raw.built_up_area_sqft = _area_from_url(raw.url)

            # Clean project name from the developer / society chip — flows through
            # _raw_to_listing into listing.raw["project_name"] which the RERA check prefers
            # over noisy title parsing. Magicbricks uses two layouts: "luxury" cards expose it
            # via developer--name--highlight; standard cards via card__society.
            if not raw.project_name:
                proj_node = card.css_first(
                    ".mb-srp__card__developer--name--highlight, .mb-srp__card__society"
                )
                if proj_node:
                    pname = proj_node.text(strip=True)
                    if pname:
                        raw.project_name = pname

    def parse_detail(self, html: str, url: str) -> RawListing:
        tree = HTMLParser(html)
        for block in _jsonld_products(tree):
            if block.get("@type") in {"Product", "Residence"}:
                raw = self._from_jsonld(block, fallback_url=url)
                if raw:
                    return raw

        # Fallback: scrape visible
        title_node = tree.css_first("h1, h2")
        title = title_node.text(strip=True) if title_node else "(untitled)"
        return RawListing(
            portal="magicbricks",
            portal_listing_id=self._id_from_url(url),
            url=url,
            title=title,
            price_inr=_parse_inr(title),
            bhk=_parse_bhk(title),
        )

    def _from_jsonld(self, item: dict, fallback_url: str = "") -> RawListing | None:
        url = item.get("url") or fallback_url
        if not url:
            return None
        name = item.get("name") or "(untitled)"
        offer = item.get("offers") or {}
        price_raw = offer.get("price") if isinstance(offer, dict) else None
        try:
            price = int(float(price_raw)) if price_raw is not None else _parse_inr(name)
        except (TypeError, ValueError):
            price = None
        floor_size = item.get("floorSize") or {}
        area = None
        if isinstance(floor_size, dict):
            try:
                area = float(floor_size.get("value")) if floor_size.get("value") else None
            except (TypeError, ValueError):
                area = None
        address = item.get("address") or {}
        locality = address.get("addressLocality") if isinstance(address, dict) else None
        return RawListing(
            portal="magicbricks",
            portal_listing_id=self._id_from_url(url),
            url=url,
            title=name,
            price_inr=price,
            built_up_area_sqft=area,
            bhk=_parse_bhk(name),
            locality=locality,
        )

    @staticmethod
    def _id_from_url(url: str) -> str:
        m = re.search(r"-pdpid-([A-Za-z0-9]+)", url) or re.search(r"[?&]id=([A-Za-z0-9]+)", url)
        if m:
            return m.group(1)
        return url.rstrip("/").rsplit("/", 1)[-1] or "unknown"

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
