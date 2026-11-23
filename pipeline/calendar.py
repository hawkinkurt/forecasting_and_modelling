"""Exact release dates and times from the ONS release calendar.

The time-series API only gives release dates, but the release calendar
(api.beta.ons.gov.uk/v1/search/releases) gives the publication time too, for
past and upcoming releases. Each ONS series names its release(s) in the
ons_release column of series.csv, e.g. "Consumer price inflation, UK"; a release
title is that name followed by ": <period>", e.g.
"Consumer price inflation, UK: November 2026".

Times are stored in London time, so summer releases show as 07:00 BST rather
than the 06:00 UTC the API returns.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from pipeline.sources.base import USER_AGENT

URL = "https://api.beta.ons.gov.uk/v1/search/releases"
LONDON = ZoneInfo("Europe/London")

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def _is_match(title, release_name):
    """'Consumer price inflation, UK: November 2026' matches, the 'time series'
    companion pages and similarly named releases do not."""
    t = title.lower()
    return (t.startswith(release_name.lower() + ":")
            and "time series" not in t and "timeseries" not in t)


def _to_london(timestamp):
    utc = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return utc.astimezone(LONDON).replace(tzinfo=None)


def _search(release_name, release_type, sort):
    response = session.get(URL, params={"query": release_name, "limit": 100,
                                        "release-type": release_type, "sort": sort}, timeout=30)
    response.raise_for_status()
    time.sleep(0.2)
    return [(x["description"]["title"], x["description"]["release_date"], x["description"].get("cancelled"),
             x.get("uri"))
            for x in response.json().get("releases", [])]


def release_times(release_name):
    """(last published, next upcoming, URL of the last published release page) for
    one release. Times are London datetimes; any item can be None."""
    published = sorted(((_to_london(d), uri) for title, d, _, uri in
                        _search(release_name, "type-published", "release_date_desc")
                        if _is_match(title, release_name)), reverse=True)
    upcoming = [_to_london(d) for title, d, cancelled, _ in
                _search(release_name, "type-upcoming", "release_date_asc")
                if _is_match(title, release_name) and not cancelled]
    last, last_uri = published[0] if published else (None, None)
    page = f"https://www.ons.gov.uk{last_uri}" if last_uri else None
    return last, (min(upcoming) if upcoming else None), page


def update_release_times(con, registry):
    """Look up every release once, then store each series' latest and next
    release time (a series in several releases takes the latest/earliest).
    Returns a list of problems (release names that could not be found)."""
    names = sorted({n for row in registry for n in row["ons_release"].split("|") if n})
    times, problems = {}, []
    for name in names:
        try:
            times[name] = release_times(name)
            if times[name][:2] == (None, None):
                problems.append(f"no releases found in the ONS calendar for '{name}'")
        except Exception as e:
            problems.append(f"release calendar lookup failed for '{name}': {type(e).__name__}: {e}")

    for row in registry:
        found = [times[n] for n in row["ons_release"].split("|") if n in times]
        if not found:
            continue
        lasts = [(t[0], t[2]) for t in found if t[0]]
        nexts = [t[1] for t in found if t[1]]
        # The most recent release, and the link to its page on the ONS website
        last_at, last_url = max(lasts) if lasts else (None, None)
        con.execute("""
            UPDATE series SET last_release_at = ?, next_release_at = ?, last_release_url = ?
            WHERE series_id = ?
        """, [last_at, min(nexts) if nexts else None, last_url, row["series_id"]])
    return problems
