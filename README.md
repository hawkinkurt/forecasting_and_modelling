# Forecasting and Modelling

Monitoring tools and forecasting models for the UK and US economies.

## What's here

- **Real-time data store.** Official statistics are pulled from the ONS and the Bank of England into a DuckDB database. Every release is kept as a separate vintage, so models can be tested on the data that was actually available at the time, not today's revised figures.
- **A daily monitoring run** that fetches every tracked series, stores only new or revised values, logs each fetch and prints what changed.
- **Power BI dashboards** built on Parquet exports of the database.
- **Forecasting models** (in progress), with every forecast stored so its accuracy can be tracked against outturns.

## How the pipeline works

```
series.csv → registry → fetchers (ONS, BoE) → vintage-aware store → SQL views → Parquet → Power BI
                                   └→ fetch log            └→ "what changed" report
```

1. **Registry.** `series.csv` lists every tracked series with its source code, frequency, units and section.
2. **Fetchers.** One module per data source, each returning the same shape: periods, values and the publication (vintage) date.
3. **Store.** Only new periods and revised values are written. A revision becomes a new row with a later vintage, so history is never overwritten.
4. **Fetch log.** Every series on every run is logged as `ok`, `no change` or `error`. One failure does not stop the run.
5. **Views and exports.** SQL views derive the latest estimates, and `export.py` writes them to Parquet for Power BI.

## Layout

| Path | Purpose |
|---|---|
| `series.csv` | The list of tracked series |
| `run.py` | Runs the whole pipeline |
| `pipeline/` | Registry, storage, report and source fetchers |
| `sql/schema.sql` | Tables: series registry, vintaged observations, fetch log, forecasts |
| `sql/views.sql` | Views such as the latest estimate for each period |
| `export.py` | Writes Parquet files to `exports/` for Power BI |
| `setup_db.py` | Creates or updates the database on its own |

## Running it

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python run.py
```

The database (`data/`) and exports (`exports/`) are rebuilt by the pipeline and are not stored in the repository.
