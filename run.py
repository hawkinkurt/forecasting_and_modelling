"""Run the whole monitor: sync the registry, fetch every series, store new or
revised data, log each fetch, export for Power BI, print what changed and
email the "what changed" report.

Usage:  .\\.venv\\Scripts\\python.exe run.py             (fetch, export and email)
        .\\.venv\\Scripts\\python.exe run.py --no-email  (skip sending; preview still saved)
"""
import argparse
from datetime import datetime

import export
from pipeline.calendar import update_release_times
from pipeline.consensus import load_consensus
from pipeline.db import connect, ensure_schema
from pipeline.email_report import build_report, save_preview, send_report
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
    parser = argparse.ArgumentParser(description="Run the UK macro monitor")
    parser.add_argument("--no-email", action="store_true", help="do not send the email report")
    args = parser.parse_args()

    registry = load_registry()

    con = connect()
    ensure_schema(con)
    sync_registry(con, registry)

    run_started = datetime.now()
    results = []
    for series in registry:
        r = fetch_one(con, series)
        print(f"{series['series_id']:<26} {r['status']:<10} {r['message'] or ''}")
        results.append(r)

    # Exact release times from the ONS release calendar
    pipeline_problems = update_release_times(con, registry)

    # Consensus figures from consensus_inbox/consensus.csv (filled by /consensus or by hand)
    captured, consensus_problems = load_consensus(con)
    pipeline_problems += consensus_problems
    if captured:
        print(f"\nConsensus loaded: {len(captured)} new figure(s)")
    for p in pipeline_problems:
        print(f"Problem: {p}")

    print_report(con, results)
    report = build_report(con, results, run_started, consensus_captured=captured,
                          extra_problems=pipeline_problems)
    con.close()  # close before exporting, which opens its own read-only connection

    print()
    export.main()

    print(f"\nEmail preview saved to {save_preview(report)}")
    if args.no_email:
        print("Email not sent (--no-email)")
    else:
        print(send_report(report))


if __name__ == "__main__":
    main()
