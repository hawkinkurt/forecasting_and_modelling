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
- `series.csv`: the registry, one row per tracked series (source, source code, dataset, name, section, frequency M/Q/A, units, measure rate/level, optional multiplier e.g. -1 to flip a sign convention). Source of truth, synced into the `series` table each run.
- `run.py`: the whole pipeline. Sync registry, fetch each series (one transaction per series, errors logged and skipped), store, update `last_fetched`/`next_release`, print the "what changed" report, export Parquet. Run manually for now.
- `pipeline/`: `db.py` (connection, `ensure_schema`), `registry.py` (load/validate/upsert), `store.py` (vintage logic), `report.py`, `sources/` (one fetcher per source returning `FetchResult(rows, vintage_date, next_release)`, registered in `sources/__init__.py`).
- `pipeline/sources/ons.py`: ONS beta API. Resolves the full URI from CDID + dataset via the search endpoint each run. Release timestamps are converted to London time (BST midnight arrives as 23:00 UTC the day before).
- `setup_db.py`: creates the tables and views on its own. Safe to re-run.
- `export.py`: writes Parquet files to `exports/` for Power BI.
- `scratch.py`: personal exploration, gitignored.

Conventions to keep: periods stored as the first day of the month, quarter or year, real-time vintages preserved (never overwrite history), series identified by a short `series_id`. Store what the source publishes (levels, or the official headline rate where that is the headline, e.g. CPI y/y and the unemployment rate) and derive everything else in SQL views. Pick the ONS dataset that is updated at every release of a series (e.g. `pn2` for quarterly GDP).

Known data notes: LFS series are labelled by the last month of the rolling 3 months. ONS `J5II` (PSNB) is published with negative = borrowing, flipped with multiplier -1. Payrolled employees (PAYE RTI) are not in the ONS time-series API and need an Excel-based fetcher.

## Roadmap (rough, revise as work progresses)

1. **UK and US macro monitor.** Pipeline and core ONS series done. Next: Bank of England source (Bank Rate, gilt yields, mortgage approvals, sterling), SQL views for derived measures, Power BI dashboard, then FRED for the US.
2. **Release charts.** Chart packs for major UK and US data releases.
3. **Nowcasting model.** UK GDP nowcast with a dynamic factor model or MIDAS in Python, tracked against outturns using the `forecasts` table and real-time vintages.
4. **Rates and fiscal.** Gilt and Treasury yield curves, bond maths, debt dynamics and fiscal rule scenarios.
5. **Commodities.** Oil price drivers and scenarios, including Middle East and energy transition angles.
