"""Render the ranked-listings Markdown report.

Two paths:
  - With ANTHROPIC_API_KEY set, ask Claude to write a 2–3 sentence per-listing narrative,
    citing evidence from the analyses. Uses prompt caching on the system prompt.
  - Without the API key (or on failure), fall back to a deterministic template. The report
    still includes the ranked table + raw evidence; just no prose.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from homeagent import db
from homeagent.config import SearchCriteria, Settings, load_criteria
from homeagent.models import Analysis, Listing

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write concise property-shortlist narratives for a discerning IT-professional
buyer in Pune. Given a listing and its verification analyses, write EXACTLY 2-3 sentences (≤ 60
words) explaining whether it's worth a closer look, citing specific evidence (RERA status, price-
vs-market, legal flags, locality fit). Be direct. No marketing fluff. No emojis.
"""


def _fmt_price(inr: int | None) -> str:
    if inr is None:
        return "—"
    if inr >= 10_000_000:
        return f"₹{inr / 10_000_000:.2f} Cr"
    if inr >= 100_000:
        return f"₹{inr / 100_000:.1f} L"
    return f"₹{inr:,}"


def _fmt_area(sqft: float | None) -> str:
    return f"{int(sqft)} sqft" if sqft else "—"


def _composite_score(analyses: list[Analysis], weights: dict[str, float]) -> float:
    total = wsum = 0.0
    for a in analyses:
        w = weights.get(a.check_name, 0.1)
        total += w * a.score
        wsum += w
    return total / wsum if wsum else 0.0


def render_report_markdown(
    listing_ids: list[int],
    criteria: SearchCriteria | None = None,
) -> str:
    criteria = criteria or load_criteria()
    settings = Settings()

    lines: list[str] = []
    lines.append("# Pune Property Shortlist")
    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    lines.append("")
    lines.append(
        f"**Criteria:** {', '.join(str(b) for b in criteria.bhk)} BHK · "
        f"₹{criteria.budget_min_cr:.1f}–{criteria.budget_max_cr:.1f} Cr · "
        f"near {criteria.proximity.anchor_name} · "
        f"localities: {', '.join(criteria.localities)}"
    )
    lines.append("")

    if not listing_ids:
        lines.append("_No verified listings to rank yet. Run `homeagent ingest` then `verify`._")
        return "\n".join(lines)

    # Summary table
    lines.append("## Ranked shortlist")
    lines.append("")
    lines.append("| # | Title | Price | Area | ₹/sqft | Locality | RERA | Pricing | Legal | Score |")
    lines.append("|---|-------|-------|------|--------|----------|------|---------|-------|-------|")

    rows: list[tuple[int, Listing, list[Analysis], float]] = []
    with db.connect() as conn:
        for rank, lid in enumerate(listing_ids, 1):
            l = db.get_listing(conn, lid)
            if not l:
                continue
            analyses = db.get_analyses(conn, lid)
            score = _composite_score(analyses, criteria.scoring_weights)
            rows.append((rank, l, analyses, score))

    for rank, l, analyses, score in rows:
        by_check = {a.check_name: a for a in analyses}

        def verdict(name: str) -> str:
            a = by_check.get(name)
            return f"{a.verdict}" if a else "—"

        pps = f"₹{int(l.price_per_sqft):,}" if l.price_per_sqft else "—"
        lines.append(
            f"| {rank} | [{l.title}]({l.url}) | {_fmt_price(l.price_inr)} | "
            f"{_fmt_area(l.built_up_area_sqft or l.carpet_area_sqft)} | {pps} | "
            f"{l.locality or '—'} | {verdict('rera_match')} | {verdict('pricing')} | "
            f"{verdict('legal')} | **{score:.2f}** |"
        )

    lines.append("")

    # Per-listing narratives
    lines.append("## Detailed analysis")
    lines.append("")
    narratives = _narratives_for(rows, settings)
    for rank, l, analyses, score in rows:
        lines.append(f"### {rank}. {l.title}  ·  composite **{score:.2f}**")
        lines.append("")
        lines.append(f"- **URL:** {l.url}")
        lines.append(f"- **Price:** {_fmt_price(l.price_inr)}  ·  **Area:** {_fmt_area(l.built_up_area_sqft or l.carpet_area_sqft)}  ·  **BHK:** {l.bhk or '—'}")
        lines.append(f"- **Locality:** {l.locality or '—'}")
        project_name = (l.raw or {}).get("project_name")
        builder = (l.raw or {}).get("builder")
        if project_name:
            lines.append(f"- **Project:** {project_name}{' (' + builder + ')' if builder else ''}")
        lines.append("")
        lines.append(narratives.get(l.id, ""))
        lines.append("")
        lines.append("**Check results:**")
        for a in analyses:
            ev = json.dumps(a.evidence, ensure_ascii=False)
            if len(ev) > 200:
                ev = ev[:200] + "…"
            lines.append(f"- `{a.check_name}` → **{a.verdict}** (score {a.score:.2f}) — {ev}")
        lines.append("")

    return "\n".join(lines)


def render_listing_detail(listing_id: int) -> str:
    with db.connect() as conn:
        l = db.get_listing(conn, listing_id)
        if not l:
            return f"No listing {listing_id}"
        analyses = db.get_analyses(conn, listing_id)
    lines = [
        f"# {l.title}",
        f"- URL: {l.url}",
        f"- Price: {_fmt_price(l.price_inr)}  ·  Area: {_fmt_area(l.built_up_area_sqft or l.carpet_area_sqft)}  ·  BHK: {l.bhk or '—'}",
        f"- Locality: {l.locality or '—'}",
        "",
        "## Checks",
    ]
    for a in analyses:
        lines.append(f"- {a.check_name}: {a.verdict} (score {a.score:.2f}) — {json.dumps(a.evidence)}")
    return "\n".join(lines)


# --------------- Claude narrative generation ---------------


def _narratives_for(rows: list[tuple], settings: Settings) -> dict[int, str]:
    """Return {listing_id: narrative}. Falls back to a template if no API key / on failure."""
    if not settings.anthropic_api_key:
        return {l.id: _template_narrative(l, analyses, score) for _, l, analyses, score in rows}

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=settings.anthropic_api_key)
        out: dict[int, str] = {}
        for _, l, analyses, score in rows:
            text = _call_claude(client, settings, l, analyses, score)
            out[l.id] = text or _template_narrative(l, analyses, score)
        return out
    except Exception as e:
        log.warning("narrative generation failed; falling back to templates: %s", e)
        return {l.id: _template_narrative(l, analyses, score) for _, l, analyses, score in rows}


def _call_claude(client, settings: Settings, listing: Listing, analyses: list[Analysis], score: float) -> str:
    payload: dict[str, Any] = {
        "title": listing.title,
        "price_inr": listing.price_inr,
        "area_sqft": listing.built_up_area_sqft or listing.carpet_area_sqft,
        "bhk": listing.bhk,
        "locality": listing.locality,
        "composite_score": round(score, 2),
        "analyses": [
            {"check": a.check_name, "verdict": a.verdict, "score": round(a.score, 2), "evidence": a.evidence}
            for a in analyses
        ],
    }

    response = client.messages.create(
        model=settings.model,
        max_tokens=200,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    blocks = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return blocks[-1].strip() if blocks else ""


def _template_narrative(listing: Listing, analyses: list[Analysis], score: float) -> str:
    by = {a.check_name: a for a in analyses}
    parts: list[str] = []

    rera = by.get("rera_match")
    if rera:
        if rera.verdict == "pass":
            rid = rera.evidence.get("rera_id", "n/a")
            parts.append(f"RERA-registered ({rid}).")
        elif rera.verdict == "fail":
            parts.append("Not found in MahaRERA — high risk.")
        elif rera.verdict == "warn":
            parts.append(f"RERA status: {rera.evidence.get('status', 'concerns')}.")

    pricing = by.get("pricing")
    if pricing and pricing.evidence.get("ratio") is not None:
        r = pricing.evidence["ratio"]
        if pricing.verdict == "pass":
            parts.append(f"Price tracks the locality median (ratio {r:.2f}).")
        elif pricing.evidence.get("flag") == "overpriced":
            parts.append(f"Priced {(r - 1) * 100:.0f}% above locality median — negotiate.")
        elif pricing.evidence.get("flag") == "suspiciously_low":
            parts.append(f"Priced {(1 - r) * 100:.0f}% below median — verify carefully.")

    legal = by.get("legal")
    if legal:
        if legal.verdict == "pass":
            parts.append("No material legal flags on the builder.")
        elif legal.verdict == "fail":
            parts.append("Builder has serious legal concerns — investigate before proceeding.")
        elif legal.verdict == "warn":
            parts.append("Minor legal/complaint history on the builder.")

    if not parts:
        parts.append(f"Composite score {score:.2f}. Insufficient analysis evidence; rerun verify.")
    return " ".join(parts)
