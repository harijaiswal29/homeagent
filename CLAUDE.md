# Project conventions for Claude

## What this is
Personal Real Estate AI Agent. See `spec.md` for the high-level brief, `README.md` for usage.

## Stack
- Python 3.12 in `.venv`. Manage deps with `pip install -e ".[dev]"` (not uv — keep it simple).
- LangGraph for orchestration. Claude (Anthropic SDK) for LLM calls. SQLite for storage.
- Playwright (sync API) for JS-heavy scrapes (Magicbricks, NoBroker); `httpx` + `selectolax`
  for simple/server-rendered pages (MahaRERA results, anything paginated).

## Conventions
- Keep LLM calls narrow. Only `verification/legal.py` and `reporting/render.py` invoke an LLM.
  Anthropic is the default; `legal.py` also supports Gemini (free tier) via
  `HOMEAGENT_LEGAL_PROVIDER=gemini`.
- Always use prompt caching on the system prompt for repeated Claude calls.
- All scraper parsers must have a fixture-backed test in `tests/fixtures/` so tests run offline.
- New verification checks go in `homeagent/verification/` and use `@register_check`.
- Use `rich` for any user-facing terminal output; use `logging` for everything else.
- Never commit `data/*.db` or `.env`.
- `db.connect()` only auto-commits the schema init — raw `conn.execute("DELETE …")` or other
  ad-hoc writes need an explicit `conn.commit()` or they'll roll back when the context exits.
  The `upsert_*` / `insert_*` helpers already commit themselves.
- Reports are dated, not overwritten. `node_render_report` writes
  `reports/YYYY-MM-DD_HHMMSS.md` AND mirrors to `reports/latest.md` when called without an
  explicit `report_path`; when callers pass `--out`/`report_path`, it writes only there and
  does NOT touch `latest.md`. Don't reintroduce a "single fixed output file" default.

## Don't
- Don't add a comm channel (WhatsApp/Email send) without explicit user ask — MVP is drafts-only.
- Don't add aggressive concurrency to scrapers; 1 req / 2–4 s + jitter is the rule.
- Don't bypass the registry by hand-calling check functions inside the graph.
- Don't put MahaRERA back on Playwright — the search results page accepts a plain GET with
  `op=Search` in the query string, so `verification/rera.py` uses httpx and paginates up to
  `_MAX_PAGES`. Form-submit via Playwright was needed in an earlier iteration; it's not now.
