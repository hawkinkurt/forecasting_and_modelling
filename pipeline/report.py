"""The "what changed" summary printed at the end of each run."""


def _fmt(value, units):
    if value is None:
        return "-"
    if units == "%":
        return f"{value:.1f}%"
    if units in ("£m", "thousands"):
        return f"{value:,.0f}"
    return f"{value:.1f}"


def _fmt_period(period, frequency):
    if frequency == "Q":
        return f"{period.year} Q{(period.month - 1) // 3 + 1}"
    if frequency == "A":
        return str(period.year)
    return f"{period:%b %Y}"


def print_report(con, results):
    """results: list of dicts with series_id, status, rows_written, message."""
    updated = [r for r in results if r["status"] == "ok"]
    unchanged = [r for r in results if r["status"] == "no change"]
    errors = [r for r in results if r["status"] == "error"]

    print("\n=== What changed ===")
    if not updated:
        print("Nothing new.")
    for r in updated:
        name, units, frequency, next_release = con.execute(
            "SELECT name, units, frequency, next_release FROM series WHERE series_id = ?",
            [r["series_id"]]).fetchone()
        latest_two = con.execute("""
            SELECT period_date, value
            FROM latest_observations
            WHERE series_id = ?
            ORDER BY period_date DESC
            LIMIT 2
        """, [r["series_id"]]).fetchall()
        period, value = latest_two[0]
        previous = latest_two[1][1] if len(latest_two) > 1 else None
        print(f"{name}: {_fmt(value, units)} in {_fmt_period(period, frequency)} "
              f"(previous period {_fmt(previous, units)}) "
              f"[{r['rows_written']} rows written, next release {next_release or 'unknown'}]")

    print(f"\nUnchanged: {len(unchanged)} series.")
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for r in errors:
            print(f"  {r['series_id']}: {r['message']}")
