import requests
import duckdb
from datetime import date, datetime

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def parse_period(label, freq):
    year_text, period_text = label.split()
    year = int(year_text)
    if freq == "M":
        return date(year, MONTHS[period_text], 1)
    if freq == "Q":
        quarter = int(period_text.replace("Q", ""))
        return date(year, 3 * (quarter - 1) + 1, 1)
    raise ValueError(f"Unknown frequency: {freq}")


def parse_ons(data, freq):
    key = "months" if freq == "M" else "quarters"
    rows = []
    for obs in data[key]:
        if obs["value"] == "":
            continue
        rows.append((parse_period(obs["date"], freq), float(obs["value"])))
    return rows


def store(con, series_id, rows, vintage_date):
    latest = dict(con.execute("""
        SELECT period_date, value FROM (
            SELECT period_date, value,
                   ROW_NUMBER() OVER (PARTITION BY period_date ORDER BY vintage_date DESC) AS rn
            FROM observations
            WHERE series_id = ?
        ) t
        WHERE rn = 1
    """, [series_id]).fetchall())

    now = datetime.now()
    to_write = []
    for period, value in rows:
        old = latest.get(period)
        if old is None or old != value:
            to_write.append((series_id, period, value, vintage_date, now))

    if to_write:
        con.executemany(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?)", to_write)
    return len(to_write)


# ---- Test ----
# Only runs when this file is run directly, not when another script imports it.
if __name__ == "__main__":
    con = duckdb.connect("data/uk_monitor.duckdb")

    uri = "/economy/inflationandpriceindices/timeseries/d7g7/mm23"
    data = requests.get("https://api.beta.ons.gov.uk/v1/data", params={"uri": uri}).json()
    rows = parse_ons(data, "M")
    vintage = date.fromisoformat(data["description"]["releaseDate"][:10])

    print("Vintage:", vintage)
    print("First run wrote:", store(con, "cpi_yoy", rows, vintage))
    print("Second run wrote:", store(con, "cpi_yoy", rows, vintage))

    con.close()