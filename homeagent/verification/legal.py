"""Legal check: web-search the builder for complaints / disputes / litigation.

Uses an LLM with a web-search tool to (1) search, (2) classify each hit's severity,
(3) return a structured JSON verdict. The provider is selectable via
`HOMEAGENT_LEGAL_PROVIDER`:
  - "anthropic" (default): Claude + `web_search_20250305`
  - "gemini": Google Gemini + `google_search` grounding (free tier on AI Studio)
Both providers produce the same JSON shape, so the on-disk cache is provider-agnostic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

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


def _builder_name(
    listing: Listing,
    project: Project | None,
    context: dict[str, Any] | None,
) -> tuple[str, str] | tuple[None, None]:
    """Resolve the builder/promoter name and where it came from.

    Returns (name, source) where source ∈ {"project", "listing_raw", "rera_promoter"},
    or (None, None) if no name is available. The source goes into evidence so the
    report explains why a verdict landed on a particular legal entity (a RERA
    promoter SPV like "Manjari Housing Projects LLP" is different from the parent
    brand "Godrej Properties").
    """
    if project and project.builder:
        return project.builder, "project"
    raw_builder = (listing.raw or {}).get("builder")
    if raw_builder:
        return raw_builder, "listing_raw"
    promoter = (context or {}).get("rera_promoter")
    if promoter:
        return promoter, "rera_promoter"
    return None, None


@register_check(name="legal", weight=0.20)
def legal_check(
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    builder, builder_source = _builder_name(listing, project, context)
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
    provider = settings.legal_provider
    caller: Callable[[str, Settings], dict[str, Any]]
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            log.warning("no ANTHROPIC_API_KEY set; skipping legal check for %r", builder)
            return CheckResult(
                verdict="unknown",
                score=0.5,
                evidence={
                    "reason": "ANTHROPIC_API_KEY not set; legal check skipped",
                    "builder": builder,
                    "provider": provider,
                },
            )
        caller = _call_claude
    elif provider == "gemini":
        if not settings.gemini_api_key:
            log.warning("no GEMINI_API_KEY set; skipping legal check for %r", builder)
            return CheckResult(
                verdict="unknown",
                score=0.5,
                evidence={
                    "reason": "GEMINI_API_KEY not set; legal check skipped",
                    "builder": builder,
                    "provider": provider,
                },
            )
        caller = _call_gemini
    else:
        log.warning("unknown legal_provider %r; skipping legal check for %r", provider, builder)
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={
                "reason": f"unknown HOMEAGENT_LEGAL_PROVIDER={provider!r}",
                "builder": builder,
            },
        )

    try:
        data = caller(builder, settings)
    except Exception as e:
        log.warning("legal check %s call failed for %s: %s", provider, builder, e)
        return CheckResult(
            verdict="unknown",
            score=0.5,
            evidence={
                "reason": f"{provider} call failed: {e}",
                "builder": builder,
                "builder_source": builder_source,
                "provider": provider,
            },
        )

    data.setdefault("provider", provider)
    data.setdefault("builder", builder)
    data.setdefault("builder_source", builder_source)
    cache.write_text(json.dumps(data))
    return CheckResult(verdict=data["verdict"], score=data["score"], evidence=data)


def _strip_json_fences(raw: str) -> str:
    """Strip ```json ... ``` (or plain ```) fences that LLMs sometimes add around JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    return raw


def _extract_json_object(raw: str) -> str:
    """Best-effort: pull the first balanced JSON object out of mixed prose+JSON output.

    Grounded responses (esp. Gemini with google_search) often answer with a prose
    summary and a trailing JSON block — or sometimes the JSON sits inside the prose.
    We scan for the first `{`, then walk forward respecting string literals and
    balancing braces until we close the object. Returns the empty string if nothing
    matches; the caller should treat that as a parse failure.
    """
    raw = _strip_json_fences(raw)
    if raw.startswith("{"):
        return raw
    start = raw.find("{")
    if start == -1:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return ""


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

    text_blocks = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    if not text_blocks:
        raise ValueError("no text response from Claude")
    return json.loads(_strip_json_fences(text_blocks[-1]))


def _call_gemini(builder: str, settings: Settings) -> dict[str, Any]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    # Gemini 2.5 Flash is a thinking model; with google_search grounding the token
    # budget is shared between thoughts, tool use, and output. Disable thinking
    # (we want JSON formatting, not reasoning) and give plenty of headroom.
    # The structured-output (response_schema) path isn't compatible with the
    # google_search tool today, so we coax JSON via the prompt and extract it.
    user_msg = (
        f"Investigate the builder/promoter: {builder} (Pune, India). "
        "Output ONLY the JSON object specified in the system instructions — "
        "no prose, no preamble, no markdown fences, no trailing text. "
        "Start your response with `{` and end with `}`."
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.0,
            # Grounded search + tool-use metadata can chew through the budget on
            # obscure entities (e.g. RERA promoter SPVs with little public footprint).
            # Gemini 2.5 Flash supports up to 65k output tokens; 32k is comfortable
            # headroom and we only pay for what's emitted.
            max_output_tokens=32768,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    raw = (response.text or "").strip()
    if not raw:
        # Fall back to walking candidates → parts (response.text can be None when
        # parts include non-text grounding metadata).
        for cand in response.candidates or []:
            parts = (cand.content.parts if cand.content else None) or []
            for part in parts:
                if getattr(part, "text", None):
                    raw = part.text.strip()
                    break
            if raw:
                break
    if not raw:
        finish = getattr((response.candidates or [None])[0], "finish_reason", None)
        raise ValueError(f"no text response from Gemini (finish_reason={finish})")
    candidate = _extract_json_object(raw)
    if not candidate:
        raise ValueError(f"no JSON object found in Gemini response (first 200 chars: {raw[:200]!r})")
    return json.loads(candidate)
