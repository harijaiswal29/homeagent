"""LangGraph state types."""

from __future__ import annotations

from typing import TypedDict

from homeagent.config import SearchCriteria


class AgentState(TypedDict, total=False):
    portal: str  # "all" | "nobroker" | "magicbricks" | "99acres"
    limit: int
    criteria: SearchCriteria
    listing_ids: list[int]  # listings touched in this run
    report_path: str
    top_n: int
    summary: dict  # counts: ingested, verified, ranked
