"""Configuration: env-driven Settings + criteria.toml loader."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="HOMEAGENT_",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    db_path: Path = Field(default=PROJECT_ROOT / "data" / "homeagent.db")
    cache_dir: Path = Field(default=PROJECT_ROOT / "data" / "cache")
    drafts_dir: Path = Field(default=PROJECT_ROOT / "drafts")
    reports_dir: Path = Field(default=PROJECT_ROOT / "reports")
    criteria_path: Path = Field(default=PROJECT_ROOT / "criteria.toml")
    model: str = Field(default="claude-sonnet-4-6")
    legal_provider: Literal["anthropic", "gemini"] = Field(default="anthropic")
    gemini_model: str = Field(default="gemini-2.5-flash")

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.cache_dir, self.drafts_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


class Proximity(BaseModel):
    anchor_name: str
    anchor_lat: float
    anchor_lng: float
    max_distance_km: float


class SearchCriteria(BaseModel):
    city: str
    bhk: list[float]
    budget_min_cr: float
    budget_max_cr: float
    property_status: list[str]
    proximity: Proximity
    localities: list[str]
    localities_exclude: list[str] = []
    scoring_weights: dict[str, float]
    rera_max_last_modified_months: int = 36

    @property
    def budget_min_inr(self) -> int:
        return int(self.budget_min_cr * 10_000_000)

    @property
    def budget_max_inr(self) -> int:
        return int(self.budget_max_cr * 10_000_000)


def load_criteria(path: Path | None = None) -> SearchCriteria:
    """Parse criteria.toml into a SearchCriteria model."""
    path = path or Settings().criteria_path
    with open(path, "rb") as f:
        data = tomllib.load(f)
    rera_cfg = data.get("rera", {}) or {}
    return SearchCriteria(
        city=data["search"]["city"],
        bhk=data["search"]["bhk"],
        budget_min_cr=data["search"]["budget_min_cr"],
        budget_max_cr=data["search"]["budget_max_cr"],
        property_status=data["search"]["property_status"],
        proximity=Proximity(**data["proximity"]),
        localities=data["localities"]["include"],
        localities_exclude=data["localities"].get("exclude", []),
        scoring_weights=dict(data["scoring"]),
        rera_max_last_modified_months=int(rera_cfg.get("max_last_modified_months", 36)),
    )
