"""Pricing check: ₹/sqft vs locality median from the current DB snapshot.

Flags listings whose price-per-sqft is significantly above (overpriced) or below (suspicious /
too-good-to-be-true) the median for the same locality. Locality median is computed at runtime
from listings currently in the DB so we adapt as the dataset grows.
"""

from __future__ import annotations

import statistics
from typing import Any

from homeagent.db import connect, list_listings
from homeagent.models import Listing, Project
from homeagent.verification.registry import CheckResult, register_check

OVERPRICED_RATIO = 1.25
UNDERPRICED_RATIO = 0.75


def _locality_pricing_baseline(locality: str | None) -> tuple[float | None, int]:
    """Return (median ₹/sqft, sample size) for listings in this locality."""
    if not locality:
        return None, 0
    with connect() as conn:
        all_listings = list_listings(conn)
    sames = [
        l.price_per_sqft
        for l in all_listings
        if l.locality and l.locality.strip().lower() == locality.strip().lower() and l.price_per_sqft
    ]
    if len(sames) < 3:  # too few samples to trust
        return None, len(sames)
    return statistics.median(sames), len(sames)


@register_check(name="pricing", weight=0.25)
def pricing_check(
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    pps = listing.price_per_sqft
    if pps is None:
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={"reason": "no price-per-sqft available (missing price or area)"},
        )

    median, sample_size = _locality_pricing_baseline(listing.locality)
    if median is None:
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={
                "reason": "insufficient locality samples for baseline",
                "locality": listing.locality,
                "sample_size": sample_size,
                "listing_pps": round(pps, 2),
            },
        )

    ratio = pps / median
    evidence = {
        "listing_pps": round(pps, 2),
        "locality_median_pps": round(median, 2),
        "ratio": round(ratio, 3),
        "sample_size": sample_size,
    }

    if ratio > OVERPRICED_RATIO:
        return CheckResult(verdict="warn", score=max(0.0, 1.5 - ratio), evidence={**evidence, "flag": "overpriced"})
    if ratio < UNDERPRICED_RATIO:
        return CheckResult(verdict="warn", score=max(0.0, ratio + 0.25), evidence={**evidence, "flag": "suspiciously_low"})
    # Within the band — score slopes off as we get further from median
    return CheckResult(verdict="pass", score=1.0 - min(0.5, abs(1.0 - ratio)), evidence=evidence)
