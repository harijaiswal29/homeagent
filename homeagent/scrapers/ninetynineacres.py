"""99acres scraper.

Mirrors the Magicbricks/NoBroker shape — JSON-LD parsing with a regex fallback.
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

BASE = "https://www.99acres.com"


class NinetyNineAcresScraper:
    name = "99acres"

    def build_search_url(self, criteria: SearchCriteria, page: int = 1) -> str:
        bhk_codes = "&".join(f"bedroom_num={int(b) if b == int(b) else b}" for b in criteria.bhk)
        return (
            f"{BASE}/search/property/buy/residential-all/{criteria.city.lower()}"
            f"?city=21&preference=S&{bhk_codes}"
            f"&budget_min={criteria.budget_min_inr}&budget_max={criteria.budget_max_inr}"
            f"&page={page}"
        )

    def search(self, criteria: SearchCriteria, limit: int = 10) -> Iterator[RawListing]:
        url = self.build_search_url(criteria)
        html = fetch_html(url)
        results = self.parse_search_results(html)
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
        tree = HTMLParser(html)
        out: list[RawListing] = []
        for node in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.text())
            except (json.JSONDecodeError, TypeError):
                continue
            blocks = data if isinstance(data, list) else [data]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("@type") == "ItemList":
                    for it in b.get("itemListElement", []) or []:
                        inner = it.get("item") if isinstance(it.get("item"), dict) else it
                        raw = self._from_jsonld(inner)
                        if raw:
                            out.append(raw)
                elif b.get("@type") in {"Product", "Residence", "RealEstateListing"}:
                    raw = self._from_jsonld(b)
                    if raw:
                        out.append(raw)
        return out

    def parse_detail(self, html: str, url: str) -> RawListing:
        results = self.parse_search_results(html)
        if results:
            # First Product block typically corresponds to the detail
            results[0].url = url or results[0].url
            return results[0]
        return RawListing(
            portal="99acres",
            portal_listing_id=self._id_from_url(url),
            url=url,
            title="(unknown)",
        )

    def _from_jsonld(self, item: dict) -> RawListing | None:
        url = item.get("url")
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
            portal="99acres",
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
        m = re.search(r"[/-]spid-([A-Za-z0-9]+)", url) or re.search(r"-r(\d+)", url)
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
