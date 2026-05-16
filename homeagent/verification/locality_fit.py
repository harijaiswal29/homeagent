"""Locality-fit check: is the listing inside the user's whitelisted localities?

Cheap, deterministic, no I/O — runs first to filter cheaply before the expensive checks.
"""

from __future__ import annotations

from typing import Any

from homeagent.config import load_criteria
from homeagent.models import Listing, Project
from homeagent.verification.registry import CheckResult, register_check


@register_check(name="locality_fit", weight=0.15)
def locality_fit_check(
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    if not listing.locality:
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={"reason": "no locality on listing"},
        )

    criteria = (context or {}).get("criteria") or load_criteria()
    wanted = [l.lower() for l in criteria.localities]
    listing_loc = listing.locality.lower()

    if any(w in listing_loc or listing_loc in w for w in wanted):
        return CheckResult(
            verdict="pass",
            score=1.0,
            evidence={"locality": listing.locality, "matched_against": criteria.localities},
        )
    return CheckResult(
        verdict="fail",
        score=0.0,
        evidence={"locality": listing.locality, "matched_against": criteria.localities},
    )
