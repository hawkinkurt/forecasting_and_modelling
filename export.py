"""Export tables and views to Parquet files in exports/ for Power BI.

Power BI cannot open a DuckDB file directly. Parquet is a compressed, typed,
columnar file format that Power BI reads natively (Get Data > Parquet), so dates
stay dates and numbers stay numbers, unlike CSV. Re-run this after each ingest,
then press Refresh in Power BI.
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "uk_monitor.duckdb"
EXPORT_DIR = ROOT / "exports"

# Each entry becomes exports/<name>.parquet
EXPORTS = {
    "latest_observations": "SELECT * FROM latest_observations_labelled ORDER BY series_id, period_date",
    "observations_all_vintages": "SELECT * FROM observations ORDER BY series_id, period_date, vintage_date",
    "series": "SELECT * FROM series ORDER BY series_id",
    "forecasts": "SELECT * FROM forecasts ORDER BY series_id, period_date, date_made",
}


def main():
    EXPORT_DIR.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    for name, query in EXPORTS.items():
        out = EXPORT_DIR / f"{name}.parquet"
        # DuckDB writes Parquet itself with COPY ... TO, no pandas needed.
        con.execute(f"COPY ({query}) TO '{out.as_posix()}' (FORMAT PARQUET)")
        rows = con.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]
        print(f"{out.name}: {rows} rows")
    con.close()


if __name__ == "__main__":
    main()
