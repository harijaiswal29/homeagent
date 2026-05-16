# Project conventions for Claude

## What this is
Personal Real Estate AI Agent. See `spec.md` for the high-level brief, `README.md` for usage.

## Stack
- Python 3.12 in `.venv`. Manage deps with `pip install -e ".[dev]"` (not uv — keep it simple).
- LangGraph for orchestration. Claude (Anthropic SDK) for LLM calls. SQLite for storage.
- Playwright (sync API) for JS-heavy scrapes; `httpx` + `selectolax` for simple ones.

## Conventions
- Keep LLM calls narrow. Only `verification/legal.py` and `reporting/render.py` invoke Claude.
- Always use prompt caching on the system prompt for repeated Claude calls.
- All scraper parsers must have a fixture-backed test in `tests/fixtures/` so tests run offline.
- New verification checks go in `homeagent/verification/` and use `@register_check`.
- Use `rich` for any user-facing terminal output; use `logging` for everything else.
- Never commit `data/*.db` or `.env`.

## Don't
- Don't add a comm channel (WhatsApp/Email send) without explicit user ask — MVP is drafts-only.
- Don't add aggressive concurrency to scrapers; 1 req / 2–4 s + jitter is the rule.
- Don't bypass the registry by hand-calling check functions inside the graph.
