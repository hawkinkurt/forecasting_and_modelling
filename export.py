"""Export tables and views to Parquet files in exports/ for Power BI.

Power BI cannot open a DuckDB file directly. Parquet is a compressed, typed,
columnar file format that Power BI reads natively (Get Data > Parquet), so dates
stay dates and numbers stay numbers, unlike CSV. run.py calls this at the end of
every run; then press Refresh in Power BI.
"""
from pipeline.db import ROOT, connect

EXPORT_DIR = ROOT / "exports"

# Each entry becomes exports/<name>.parquet
EXPORTS = {
    # Raw data and metadata
    "latest_observations": "SELECT * FROM latest_observations_labelled ORDER BY series_id, period_date",
    "observations_all_vintages": "SELECT * FROM observations ORDER BY series_id, period_date, vintage_date",
    "series": "SELECT * FROM series ORDER BY section, series_id",
    "fetch_log": "SELECT * FROM fetch_log ORDER BY fetched_at",
    "forecasts": "SELECT * FROM forecasts ORDER BY series_id, period_date, date_made",
    # Derived measures (see sql/views.sql)
    "changes": "SELECT * FROM changes ORDER BY series_id, period_date",
    "real_pay": "SELECT * FROM real_pay ORDER BY series_id, period_date",
    "yield_curve": "SELECT * FROM yield_curve ORDER BY period_date",
    "real_policy_rate": "SELECT * FROM real_policy_rate ORDER BY period_date",
    "psnb_fiscal_year": "SELECT * FROM psnb_fiscal_year ORDER BY period_date",
    "revisions": "SELECT * FROM revisions ORDER BY series_id, period_date",
    # Dashboard summaries
    "latest_snapshot": "SELECT * FROM latest_snapshot ORDER BY section, series_id",
    "data_health": "SELECT * FROM data_health ORDER BY section, series_id",
}


def main():
    EXPORT_DIR.mkdir(exist_ok=True)
    con = connect(read_only=True)
    for name, query in EXPORTS.items():
        out = EXPORT_DIR / f"{name}.parquet"
        # DuckDB writes Parquet itself with COPY ... TO, no pandas needed.
        con.execute(f"COPY ({query}) TO '{out.as_posix()}' (FORMAT PARQUET)")
        rows = con.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]
        print(f"exported {out.name}: {rows} rows")
    con.close()


if __name__ == "__main__":
    main()
