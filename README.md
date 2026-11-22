# Forecasting and Modelling

Monitoring tools and forecasting models for the UK and US economies.

## What's here

- **Real-time data store.** Official statistics are pulled from source APIs (starting with the ONS) into a DuckDB database. Every release is kept as a separate vintage, so models can be tested on the data that was actually available at the time, not today's revised figures.
- **Power BI dashboards** built on Parquet exports of the database.
- **Forecasting models** (in progress), with every forecast stored so its accuracy can be tracked against outturns.

## Layout

| Path | Purpose |
|---|---|
| `sql/schema.sql` | Tables: series registry, vintaged observations, fetch log, forecasts |
| `sql/views.sql` | Views such as the latest estimate for each period |
| `setup_db.py` | Creates or updates the database |
| `ingest.py` | Fetches series from the ONS API and stores new or revised values |
| `export.py` | Writes Parquet files to `exports/` for Power BI |

## Running it

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python setup_db.py
.\.venv\Scripts\python ingest.py
.\.venv\Scripts\python export.py
```

The database (`data/`) and exports (`exports/`) are rebuilt by these scripts and are not stored in the repository.
