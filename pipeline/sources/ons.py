"""ONS time series via the ONS beta API (no key needed).

A series is identified by its CDID (e.g. D7G7) and the dataset it is published
in (e.g. mm23). The same CDID can appear in several datasets that are updated on
different days, so the dataset matters.
"""
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from pipeline.sources.base import FetchResult

API = "https://api.beta.ons.gov.uk/v1"
LONDON = ZoneInfo("Europe/London")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
FREQ_KEYS = {"M": "months", "Q": "quarters", "A": "years"}

session = requests.Session()
session.headers["User-Agent"] = "forecasting-and-modelling/0.1 (personal research)"


def _get(path, params):
    response = session.get(f"{API}{path}", params=params, timeout=30)
    response.raise_for_status()
    time.sleep(0.2)  # be polite to the API
    return response.json()


def resolve_uri(cdid, dataset):
    """Find the full ONS URI (it includes a topic path) for a CDID in a dataset."""
    items = _get("/search", {"content_type": "timeseries", "cdids": cdid}).get("items", [])
    suffix = f"/{cdid.lower()}/{dataset.lower()}"
    for item in items:
        if item["uri"].endswith(suffix):
            return item["uri"]
    found = ", ".join(i["uri"].rsplit("/", 1)[-1] for i in items) or "none"
    raise LookupError(f"{cdid} not found in dataset {dataset} (datasets found: {found})")


def parse_period(label, freq):
    """'2026 OCT' -> 2026-10-01, '2026 Q3' -> 2026-07-01, '2026' -> 2026-01-01."""
    parts = label.split()
    year = int(parts[0])
    if freq == "A":
        return date(year, 1, 1)
    if freq == "M":
        return date(year, MONTHS[parts[1].upper()], 1)
    if freq == "Q":
        quarter = int(parts[1].upper().replace("Q", ""))
        return date(year, 3 * (quarter - 1) + 1, 1)
    raise ValueError(f"Unknown frequency: {freq}")


def parse_release_date(timestamp):
    """ONS gives release dates as UTC timestamps of midnight London time.

    In summer (BST) that is 23:00 UTC the day before, so taking the first 10
    characters would put the release a day early. Converting to London time
    first gives the right date all year round.
    """
    utc = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return utc.astimezone(LONDON).date()


def parse_next_release(text):
    """'22 December 2026' -> date. Returns None for 'To be announced' etc."""
    try:
        return datetime.strptime(text.strip(), "%d %B %Y").date()
    except (AttributeError, ValueError):
        return None


def fetch(series):
    uri = resolve_uri(series["source_code"], series["dataset"])
    data = _get("/data", {"uri": uri})

    freq = series["frequency"]
    rows = []
    for obs in data.get(FREQ_KEYS[freq]) or []:
        try:
            value = float(obs["value"])
        except (TypeError, ValueError):
            continue  # blank or suppressed value
        rows.append((parse_period(obs["date"], freq), value))
    if not rows:
        raise ValueError(f"No {FREQ_KEYS[freq]} data returned for {uri}")

    description = data["description"]
    return FetchResult(
        rows=rows,
        vintage_date=parse_release_date(description["releaseDate"]),
        next_release=parse_next_release(description.get("nextRelease")),
    )
