"""Legal check: web-search the builder for complaints / disputes / litigation.

Uses Claude's `web_search_20250305` tool. Claude is asked to (1) search, (2) classify each hit's
severity, (3) return a structured JSON verdict. This is the only check that costs API tokens —
so we cache by builder name on disk for the session.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from homeagent.config import Settings
from homeagent.models import Listing, Project
from homeagent.verification.registry import CheckResult, register_check

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a real-estate due-diligence assistant. The user will give you the name
of a builder/promoter in Pune, India. Use web search to find any of the following from the past
3 years:
  - consumer complaints
  - litigation / court cases
  - RERA penalties
  - news of project delays or defaults
  - fraud allegations

Respond with a single JSON object only (no prose), matching this schema:
{
  "verdict": "pass" | "warn" | "fail",
  "score": <float 0.0..1.0>,
  "summary": "<2-3 sentence summary of findings>",
  "incidents": [
    {"title": "...", "url": "...", "severity": "low" | "med" | "high"}
  ]
}
Rules:
  - "pass" + score≥0.85 when nothing material is found.
  - "warn" + score 0.4–0.7 for low-severity grumbles (delays, isolated complaints).
  - "fail" + score≤0.3 for fraud allegations, multiple high-severity items, RERA penalties.
  - Cap "incidents" at 5 most-severe.
"""


def _cache_path(builder: str) -> Path:
    s = Settings()
    s.ensure_dirs()
    safe = "".join(c if c.isalnum() else "_" for c in builder.lower())[:60]
    return s.cache_dir / f"legal_{safe}.json"


def _builder_name(listing: Listing, project: Project | None) -> str | None:
    if project and project.builder:
        return project.builder
    return (listing.raw or {}).get("builder")


@register_check(name="legal", weight=0.20)
def legal_check(
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    builder = _builder_name(listing, project)
    if not builder:
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={"reason": "no builder name on listing"},
        )

    cache = _cache_path(builder)
    if cache.exists():
        data = json.loads(cache.read_text())
        return CheckResult(verdict=data["verdict"], score=data["score"], evidence=data)

    # Allow tests to inject a fake result without hitting Claude
    inject = (context or {}).get("legal_result")
    if inject is not None:
        cache.write_text(json.dumps(inject))
        return CheckResult(verdict=inject["verdict"], score=inject["score"], evidence=inject)

    settings = Settings()
    if not settings.anthropic_api_key:
        log.warning("no ANTHROPIC_API_KEY set; skipping legal check for %r", builder)
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={"reason": "ANTHROPIC_API_KEY not set; legal check skipped", "builder": builder},
        )

    try:
        data = _call_claude(builder, settings)
    except Exception as e:
        log.warning("legal check Claude call failed for %s: %s", builder, e)
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={"reason": f"claude call failed: {e}", "builder": builder},
        )

    cache.write_text(json.dumps(data))
    return CheckResult(verdict=data["verdict"], score=data["score"], evidence=data)


def _call_claude(builder: str, settings: Settings) -> dict[str, Any]:
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.model,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": f"Investigate the builder/promoter: {builder} (Pune, India)."}],
    )

    # Find the final text block (the JSON response)
    text_blocks = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    if not text_blocks:
        raise ValueError("no text response from Claude")
    raw = text_blocks[-1].strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    return json.loads(raw)
