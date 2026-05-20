"""Scraper protocol + shared helpers (Playwright launcher, rate limiting, HTML caching)."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from homeagent.config import Settings

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


@dataclass
class SearchCriteria:
    """Parameters a scraper needs to issue a search."""

    city: str
    bhk: list[float]
    budget_min_inr: int
    budget_max_inr: int
    localities: list[str]
    property_status: list[str] = field(default_factory=lambda: ["new", "resale"])
    localities_exclude: list[str] = field(default_factory=list)
    # Source-specific knobs (currently only consumed by the MahaRERA scraper).
    rera_max_last_modified_months: int = 36


@dataclass
class RawListing:
    """What a scraper's `parse_*` functions return — pre-DB shape."""

    portal: str
    portal_listing_id: str
    url: str
    title: str
    price_inr: int | None = None
    carpet_area_sqft: float | None = None
    built_up_area_sqft: float | None = None
    bhk: float | None = None
    locality: str | None = None
    project_name: str | None = None
    builder: str | None = None
    status: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)


class Scraper(Protocol):
    """All portal scrapers implement this."""

    name: str

    def search(self, criteria: SearchCriteria, limit: int = 10) -> Iterator[RawListing]:
        """Yield summary listings matching the criteria."""

    def fetch_detail(self, url: str) -> RawListing:
        """Fetch and parse a single listing detail page."""

    def parse_search_results(self, html: str, base_url: str = "") -> list[RawListing]:
        """Pure HTML→listings; exposed so tests can run offline against fixtures."""

    def parse_detail(self, html: str, url: str) -> RawListing:
        """Pure HTML→detail listing; exposed for offline fixture testing."""


# --------------- Shared helpers ---------------


def cache_path(url: str, settings: Settings | None = None) -> Path:
    s = settings or Settings()
    s.ensure_dirs()
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return s.cache_dir / f"{h}.html"


def load_cached(url: str, settings: Settings | None = None) -> str | None:
    p = cache_path(url, settings)
    return p.read_text(encoding="utf-8") if p.exists() else None


def save_cache(url: str, html: str, settings: Settings | None = None) -> None:
    p = cache_path(url, settings)
    p.write_text(html, encoding="utf-8")


def polite_sleep(min_s: float = 2.0, max_s: float = 4.0) -> None:
    """Sleep a random interval between requests — be a good citizen."""
    time.sleep(random.uniform(min_s, max_s))


@contextmanager
def playwright_browser(headless: bool = True):
    """Yield a Playwright browser + context with a random UA + stealth-ish args.

    Imported lazily so `pytest` can run without Playwright actually installed in CI.
    """
    from playwright.sync_api import sync_playwright

    ua = random.choice(USER_AGENTS)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=ua,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
        )
        try:
            yield context
        finally:
            context.close()
            browser.close()


def fetch_html(url: str, use_cache: bool = True, headless: bool = True) -> str:
    """Fetch a URL with caching; uses Playwright for JS-rendered content."""
    if use_cache:
        cached = load_cached(url)
        if cached:
            log.info("cache hit %s", url)
            return cached

    log.info("fetching %s", url)
    with playwright_browser(headless=headless) as context:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Best-effort wait for late-loading content; tolerate timeout
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        html = page.content()

    save_cache(url, html)
    polite_sleep()
    return html
