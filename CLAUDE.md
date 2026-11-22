# Forecasting and Modelling

Monitoring tools, forecasting models and chart packs on the UK and US economies (plus other areas over time). Personal context about the author lives in `CLAUDE.local.md`, which is gitignored.

## How we work

- **Claude writes essentially all of the code.** The author directs the economics, decides what to build and reviews results.
- **Claude does no writing in the author's voice.** Substack notes, commentary and other prose are the author's own. Don't draft them, even when asked for charts or analysis to go with a note. Technical docs (READMEs, docstrings, commit messages) are fine.
- **Explain as you go**, briefly. When introducing a new method, library, SQL technique or finance concept, say in a sentence or two what it is and why it fits. Prefer clear, readable code that could be followed and defended in an interview over clever code. Lean on SQL in DuckDB for data work where it is natural.

## Environment

- Windows 11, PyCharm, Python 3.12 in `.venv`. Run scripts with `.\.venv\Scripts\python.exe`. Shell commands should work in PowerShell.
- Keep `requirements.txt` pinned and updated when adding packages.
- Database: DuckDB at `data/uk_monitor.duckdb` (gitignored, along with `exports/`).
- Power BI Desktop for dashboards. Power BI does not read DuckDB, so `export.py` writes Parquet files to `exports/` and Power BI reads those. After an ingest: run `export.py`, then Refresh in Power BI.
- API keys go in `.env` (gitignored), never in code.
- Public GitHub repository used as a portfolio piece. Commits, READMEs and code should look professional. Never commit personal details, data files or secrets.

## Current state of the code

- `sql/schema.sql`: tables `series` (metadata), `observations` (vintage-aware: one row per series, period and vintage date), `fetch_log`, `forecasts` (keeps every forecast with date made and model, for later accuracy tracking).
- `sql/views.sql`: `latest_observations` (most recent vintage per period) and `latest_observations_labelled` (joined to `series`, long format for Power BI).
- `setup_db.py`: creates the tables and views. Safe to re-run.
- `ingest.py`: ONS time-series fetch via `api.beta.ons.gov.uk/v1/data`, parses monthly and quarterly periods, and `store()` writes only new or revised values under the release date as vintage. Currently tested on CPI 12-month rate (D7G7, MM23) as `cpi_yoy`.
- `export.py`: writes Parquet files to `exports/` for Power BI.
- `scratch.py`: personal exploration, gitignored.

Conventions to keep: periods stored as the first day of the month or quarter, real-time vintages preserved (never overwrite history), series identified by a short `series_id` mapped to the source code in `series`.

## Roadmap (rough, revise as work progresses)

1. **UK and US macro monitor.** Finish the ingest pipeline (series registry, fetch log, more ONS series, then FRED and Bank of England sources), SQL views for derived measures, Power BI dashboard on top.
2. **Release charts.** Chart packs for major UK and US data releases.
3. **Nowcasting model.** UK GDP nowcast with a dynamic factor model or MIDAS in Python, tracked against outturns using the `forecasts` table and real-time vintages.
4. **Rates and fiscal.** Gilt and Treasury yield curves, bond maths, debt dynamics and fiscal rule scenarios.
5. **Commodities.** Oil price drivers and scenarios, including Middle East and energy transition angles.
