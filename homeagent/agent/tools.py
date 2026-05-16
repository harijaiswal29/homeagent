"""Tool definitions exposed to Claude during the report-render node.

Each function below has a corresponding tool schema. The renderer can call these to fetch
listing/analysis context without us having to stuff everything into the prompt.
"""

from __future__ import annotations

from typing import Any

from homeagent import db


def get_listing(listing_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        l = db.get_listing(conn, listing_id)
    if not l:
        return {"error": f"no listing {listing_id}"}
    return l.model_dump(mode="json")


def get_analyses(listing_id: int) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [a.model_dump(mode="json") for a in db.get_analyses(conn, listing_id)]


TOOL_SCHEMAS = [
    {
        "name": "get_listing",
        "description": "Fetch a listing by id (returns the full row including title, price, area, locality, and raw payload).",
        "input_schema": {
            "type": "object",
            "properties": {"listing_id": {"type": "integer"}},
            "required": ["listing_id"],
        },
    },
    {
        "name": "get_analyses",
        "description": "Fetch all check results for a listing (rera_match, pricing, legal, locality_fit, ...).",
        "input_schema": {
            "type": "object",
            "properties": {"listing_id": {"type": "integer"}},
            "required": ["listing_id"],
        },
    },
]


TOOLS = {"get_listing": get_listing, "get_analyses": get_analyses}
