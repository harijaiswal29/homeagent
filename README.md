# homeagent

Real Estate AI Agent that automates a Pune house search: scrape portals → dedupe → cross-check
against MahaRERA → flag legal/pricing concerns → produce a ranked Markdown report.

## Quick start

```bash
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env
# edit .env and add your ANTHROPIC_API_KEY

# review and adjust search settings
$EDITOR criteria.toml

# run
python -m homeagent ingest --portal nobroker --limit 10
python -m homeagent verify --all-unverified
python -m homeagent report --top 10 --out reports/latest.md
```

Or all at once:

```bash
python -m homeagent run-all
```

## Architecture

```
ingest (scrapers/) → dedupe → enrich (verification/rera) → run_checks (registry)
                                                              ├─ pricing
                                                              ├─ legal (Claude)
                                                              └─ locality_fit
                                                                      ↓
                                                       score + rank → render_report (Claude)
```

Orchestrated by LangGraph in `homeagent/agent/graph.py`. SQLite is the single source of truth
(`data/homeagent.db`).

## Adding a new check

Drop a file into `homeagent/verification/` and decorate it:

```python
from homeagent.verification.registry import register_check, CheckResult

@register_check(name="metro_distance", weight=0.10)
def metro_distance(listing, project) -> CheckResult:
    ...
    return CheckResult(verdict="pass", score=0.9, evidence={"km": 0.8})
```

The registry auto-discovers it on next run. Update weights in `criteria.toml`.

## Caveats

- **Portal scraping**: Magicbricks / NoBroker / 99acres ToS prohibit scraping. This tool is for
  personal, low-volume, educational use. Do not run at high frequency, do not redistribute the
  scraped data.
- **WhatsApp/Email outreach**: not enabled in MVP. The `comms/drafts.py` module produces ready-to-
  send draft text for you to review and send manually.
- **MahaRERA**: portal layouts change; if RERA lookups start failing, refresh fixtures and parsers.
