"""Pydantic models — the single source of truth for shapes flowing through the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)

Verdict = Literal["pass", "warn", "fail", "unknown"]
PropertyStatus = Literal["new", "resale", "unknown"]


class Project(BaseModel):
    id: int | None = None
    name: str
    builder: str | None = None
    rera_id: str | None = None
    locality: str | None = None
    lat: float | None = None
    lng: float | None = None


class Listing(BaseModel):
    id: int | None = None
    portal: str
    portal_listing_id: str  # portal-native ID (e.g. NoBroker's property id)
    url: str  # stored as str — sqlite doesn't care, validation by HttpUrl is overkill at boundary
    title: str
    price_inr: int | None = None  # absolute rupees
    carpet_area_sqft: float | None = None
    built_up_area_sqft: float | None = None
    bhk: float | None = None
    locality: str | None = None
    status: PropertyStatus = "unknown"
    project_id: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    scraped_at: datetime = Field(default_factory=_utcnow)

    @property
    def price_per_sqft(self) -> float | None:
        area = self.carpet_area_sqft or self.built_up_area_sqft
        if not area or not self.price_inr:
            return None
        return self.price_inr / area


class RERAEntry(BaseModel):
    id: int | None = None
    rera_id: str
    project_name: str
    promoter: str | None = None
    status: str | None = None  # e.g. "Registered", "Lapsed", "Revoked"
    completion_date: str | None = None  # keep as ISO date string for portability
    complaints_count: int | None = None
    fetched_at: datetime = Field(default_factory=_utcnow)
    raw_html: str | None = None


class Analysis(BaseModel):
    id: int | None = None
    listing_id: int
    check_name: str
    verdict: Verdict
    score: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    run_at: datetime = Field(default_factory=_utcnow)


class Report(BaseModel):
    id: int | None = None
    generated_at: datetime = Field(default_factory=_utcnow)
    criteria_snapshot: dict[str, Any]
    ranked_listing_ids: list[int]
    markdown: str
