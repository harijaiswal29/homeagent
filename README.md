# homeagent

Real Estate AI Agent that automates a Pune house search: scrape portals → dedupe → cross-check
against MahaRERA → flag pricing and legal concerns → produce a ranked Markdown report.

## Quick start

```bash
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium               # for the Magicbricks/NoBroker scrapers
sudo playwright install-deps chromium     # one-time system libs on Linux/WSL

cp .env.example .env
# (optional) edit .env and add your ANTHROPIC_API_KEY — without it, the legal check
# returns "unknown" and report narratives fall back to a deterministic template.

# review and adjust search settings
$EDITOR criteria.toml

# run end-to-end against live data (Magicbricks is the most reliable portal today)
python -m homeagent ingest --portal magicbricks --limit 15
python -m homeagent verify --all-unverified
python -m homeagent report --top 10 --out reports/latest.md
```

Or all at once:

```bash
python -m homeagent run-all --portal magicbricks --limit 15 --top 10
```

### Offline demo

For a no-network smoke test that exercises the full pipeline against HTML fixtures and a
seeded RERA cache:

```bash
python -m homeagent demo
```

## Architecture

```
ingest → dedupe → run_checks → score + rank → render_report
            ↑          ↑                             ↑
       scrapers/  verification/registry        reporting/render
                  (rera_match, pricing,        (Claude or template)
                   legal, locality_fit)
```

Orchestrated by LangGraph in `homeagent/agent/graph.py`. SQLite is the single source of truth
(`data/homeagent.db`). All four built-in checks run from the registry — no hand-wiring.

## Adding a new check

Drop a file into `homeagent/verification/` and decorate it:

```python
from typing import Any
from homeagent.models import Listing, Project
from homeagent.verification.registry import register_check, CheckResult

@register_check(name="metro_distance", weight=0.10)
def metro_distance(
    listing: Listing,
    project: Project | None = None,
    context: dict[str, Any] | None = None,
) -> CheckResult:
    ...
    return CheckResult(verdict="pass", score=0.9, evidence={"km": 0.8})
```

The registry auto-discovers it on next run. Adjust the per-check weights in `criteria.toml`.

## Portal status

| Portal      | Live scrape | Notes                                                            |
|-------------|-------------|------------------------------------------------------------------|
| Magicbricks | ✅ working  | JSON-LD `Apartment` blocks + DOM-card merge for price/area       |
| 99acres     | ⚠️ untested | Same architecture as Magicbricks; selectors may need a refresh   |
| NoBroker    | ❌ SPA wall | Search URLs need a base64-encoded `searchParam` blob; deferred   |

For NoBroker, the manual-capture path works today: save a search-results HTML in your browser,
drop it into `data/cache/`, and the existing parser will consume it offline.

## Caveats

- **Portal ToS**: Magicbricks / NoBroker / 99acres terms of service prohibit scraping. This
  tool is for personal, low-volume, educational use. Don't run at high frequency, don't
  redistribute the scraped data.
- **MahaRERA fragility**: the verifier hits the public `/projects-search-result` page via
  httpx and paginates up to `_MAX_PAGES=15` (~150 results). If MahaRERA changes the form URL
  or the result-card markup, refresh `tests/fixtures/maharera_search*.html` and adjust
  `homeagent/verification/rera.py`. Detail-page fetch is intentionally skipped — search cards
  already carry rera_id, project name, promoter, and district.
- **WhatsApp/Email outreach**: not enabled in MVP. The `comms/drafts.py` module is a stub for
  drafts-only output you'd send manually.
