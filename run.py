"""Run the whole monitor: sync the registry, fetch every series, store new or
revised data, log each fetch, export for Power BI and print what changed.

Usage:  .\\.venv\\Scripts\\python.exe run.py
"""
from datetime import datetime

import export
from pipeline.db import connect, ensure_schema
from pipeline.registry import load_registry, sync_registry
from pipeline.report import print_report
from pipeline.sources import FETCHERS
from pipeline.store import store


def fetch_one(con, series):
    """Fetch and store one series. Returns a result dict; never raises."""
    series_id = series["series_id"]
    result = {"series_id": series_id, "status": "error",
              "rows_received": None, "rows_written": None, "message": None}
    # A transaction makes each series all-or-nothing: if anything fails part
    # way through, ROLLBACK undoes that series' writes and the database is left
    # as it was before this series.
    con.begin()
    try:
        fetched = FETCHERS[series["source"]](series)
        rows = fetched.rows
        if series["multiplier"] != 1:
            rows = [(period, value * series["multiplier"]) for period, value in rows]
        written = store(con, series_id, rows, fetched.vintage_date)
        con.execute("UPDATE series SET last_fetched = ?, next_release = ? WHERE series_id = ?",
                    [datetime.now(), fetched.next_release, series_id])
        con.commit()
        result.update(status="ok" if written else "no change",
                      rows_received=len(fetched.rows), rows_written=written,
                      message=f"vintage {fetched.vintage_date}")
    except Exception as e:  # one bad series must not stop the others
        con.rollback()
        result["message"] = f"{type(e).__name__}: {e}"

    con.execute("INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?)",
                [datetime.now(), series_id, result["status"],
                 result["rows_received"], result["rows_written"], result["message"]])
    return result


def main():
    registry = load_registry()

    con = connect()
    ensure_schema(con)
    sync_registry(con, registry)

    results = []
    for series in registry:
        r = fetch_one(con, series)
        print(f"{series['series_id']:<26} {r['status']:<10} {r['message'] or ''}")
        results.append(r)

    print_report(con, results)
    con.close()  # close before exporting, which opens its own read-only connection

    print()
    export.main()


if __name__ == "__main__":
    main()
