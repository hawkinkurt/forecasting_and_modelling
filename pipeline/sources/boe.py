"""Bank of England Interactive Statistical Database (IADB), no key needed.

Series are identified by a seven or eight letter code (e.g. IUDBEDR for Bank
Rate). The database has a CSV download URL that takes the codes and a date
range. It does not say when data were published, so the vintage is the date we
fetched it: a revision shows up as a new vintage on the day we first see it.
Daily market data (rates, yields, exchange rates) are not revised anyway.
"""
import csv
import io
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from pipeline.sources.base import USER_AGENT, FetchResult

URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
# Earliest start date the IADB accepts (an earlier one returns an error page).
# Series that begin later simply start at their first observation.
START = "01/Jan/1963"
LONDON = ZoneInfo("Europe/London")

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def parse_period(label, freq):
    """'19 Nov 2026' -> date. The BoE dates monthly data at month end, so for
    M/Q/A we move to the first day of the period to match our convention."""
    d = datetime.strptime(label.strip(), "%d %b %Y").date()
    if freq == "D":
        return d
    if freq == "M":
        return d.replace(day=1)
    if freq == "Q":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if freq == "A":
        return date(d.year, 1, 1)
    raise ValueError(f"Unknown frequency: {freq}")


def fetch(series):
    code = series["source_code"]
    params = {
        "csv.x": "yes",
        "Datefrom": START,
        "Dateto": "now",
        "SeriesCodes": code,
        "CSVF": "TN",       # tabular layout, no title rows: DATE,<code>
        "UsingCodes": "Y",
        "VPD": "Y",         # include provisional data
        "VFD": "N",         # no footnotes
    }
    response = session.get(URL, params=params, timeout=60)
    response.raise_for_status()
    time.sleep(0.5)  # be polite to the server

    # Bad requests (unknown code, invalid date) return an HTML error page with
    # status 200 rather than a proper error, so check what came back.
    if "csv" not in response.headers.get("content-type", ""):
        raise LookupError(f"BoE returned an error page instead of CSV for {code} "
                          f"(unknown code or invalid request)")

    reader = csv.DictReader(io.StringIO(response.text))
    rows = []
    for record in reader:
        try:
            value = float(record[code])
        except (KeyError, TypeError, ValueError):
            continue  # blank: no observation on that date
        rows.append((parse_period(record["DATE"], series["frequency"]), value))
    if not rows:
        raise ValueError(f"No data returned for {code}")

    return FetchResult(
        rows=rows,
        vintage_date=datetime.now(LONDON).date(),
        next_release=None,  # the IADB does not publish a release calendar
    )
