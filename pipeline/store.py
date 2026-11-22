"""Vintage-aware storage of observations."""
from datetime import datetime


def store(con, series_id, rows, vintage_date):
    """Write only new periods and revised values, tagged with their vintage.

    rows: list of (period_date, value). Returns the number of rows written.
    History is never overwritten: a revision is a new row with a later
    vintage_date, so every past estimate stays queryable.
    """
    # Latest value we hold for each period (the most recent vintage).
    latest = dict(con.execute("""
        SELECT period_date, value
        FROM observations
        WHERE series_id = ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY period_date ORDER BY vintage_date DESC) = 1
    """, [series_id]).fetchall())

    now = datetime.now()
    to_write = [
        (series_id, period, value, vintage_date, now)
        for period, value in rows
        if latest.get(period) != value
    ]

    if to_write:
        # INSERT OR REPLACE: if a row with the same (series, period, vintage)
        # already exists, e.g. the source corrected a figure on release day,
        # replace it rather than failing on the primary key.
        con.executemany(
            "INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?)", to_write)
    return len(to_write)
