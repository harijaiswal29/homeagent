"""RERA check: was the project found in MahaRERA, and is it in good standing?

Wraps `homeagent.verification.rera.lookup_project` and translates the result into a CheckResult.
"""

from __future__ import annotations

from typing import Any

from homeagent.models import Listing, Project
from homeagent.verification import rera
from homeagent.verification.registry import CheckResult, register_check


def _project_name(listing: Listing, project: Project | None) -> str | None:
    if project and project.name:
        return project.name
    raw_name = listing.raw.get("project_name") if listing.raw else None
    if raw_name:
        return raw_name
    # Fall back to extracting from the title: "3 BHK in Skyline Heights, Kharadi" → "Skyline Heights"
    title = listing.title or ""
    if " in " in title:
        after = title.split(" in ", 1)[1]
        return after.split(",")[0].strip()
    return None


@register_check(name="rera_match", weight=0.40)
def rera_match_check(
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    name = _project_name(listing, project)
    if not name:
        return CheckResult(
            verdict="unknown",
            score=0.3,
            evidence={"reason": "no project name to look up"},
        )

    # Tests / offline runs can inject a precomputed entry via context
    entry = (context or {}).get("rera_entry")
    if entry is None:
        entry = rera.lookup_project(name)

    if entry is None:
        return CheckResult(
            verdict="fail",
            score=0.0,
            evidence={"queried": name, "reason": "no MahaRERA match found"},
        )

    status = (entry.status or "").lower()
    evidence = {
        "rera_id": entry.rera_id,
        "matched_project": entry.project_name,
        "promoter": entry.promoter,
        "status": entry.status,
        "complaints": entry.complaints_count,
    }

    if "register" in status:
        score = 1.0
        # Penalise high complaint counts
        if entry.complaints_count and entry.complaints_count > 5:
            score = 0.7
        return CheckResult(verdict="pass", score=score, evidence=evidence)

    if "lapsed" in status or "expire" in status:
        return CheckResult(verdict="warn", score=0.4, evidence=evidence)

    if "revoke" in status or "cancel" in status:
        return CheckResult(verdict="fail", score=0.0, evidence=evidence)

    return CheckResult(verdict="warn", score=0.5, evidence=evidence)
