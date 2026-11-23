"""The "what changed" email sent after each run.

Sections:
  1. New releases: monthly and quarterly series with a new period this run.
  2. Markets: daily series (rates, yields, sterling).
  3. Revisions: earlier periods whose values changed this run.
  4. Coming up: scheduled releases in the next 7 days.
  5. Problems: failed fetches and series flagged by the data_health view.

What is new or revised is worked out by comparing the rows written this run
(fetched_at >= run start) with the previous vintage of the same period, so the
email comes straight from the vintage-aware store.

Sending uses the Gmail API (see pipeline/gmail.py), with the sender and recipient
from .env. Every run also saves an HTML preview to exports/last_report.html.
"""
import base64
import html
import io
import os
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import urlencode

import matplotlib

matplotlib.use("Agg")  # draw to images without opening windows
import matplotlib.pyplot as plt  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from pipeline import gmail  # noqa: E402
from pipeline.db import ROOT  # noqa: E402
from pipeline.registry import load_registry  # noqa: E402
from pipeline.report import _fmt, _fmt_period  # noqa: E402

PREVIEW_PATH = ROOT / "exports" / "last_report.html"
CALENDAR_URL = "https://www.bloomberg.com/markets/economic-calendar"

# How far back a revision is worth reporting. Seasonal adjustment revises
# whole histories by tiny amounts every release; those are counted, not listed.
REVISION_WINDOW_MONTHS = {"D": 1, "M": 6, "Q": 12, "A": 24}

COLOURS = {"text": "#1f2328", "muted": "#656d76", "rule": "#d0d7de", "head": "#f6f8fa",
           "down": "#cf222e", "line": "#0969da"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def changes_this_run(con, run_started):
    """Every row written this run, with the value it replaced (None if new)."""
    return con.execute("""
        WITH this_run AS (
            SELECT series_id, period_date, value
            FROM observations
            WHERE fetched_at >= ?
        ),
        previous AS (
            SELECT o.series_id, o.period_date, o.value
            FROM observations AS o
            JOIN this_run AS t USING (series_id, period_date)
            WHERE o.fetched_at < ?
            QUALIFY ROW_NUMBER() OVER (PARTITION BY o.series_id, o.period_date
                                       ORDER BY o.vintage_date DESC) = 1
        )
        SELECT t.series_id, t.period_date, t.value AS new_value, p.value AS old_value
        FROM this_run AS t
        LEFT JOIN previous AS p USING (series_id, period_date)
        ORDER BY t.series_id, t.period_date
    """, [run_started, run_started]).fetchall()


def series_info(con):
    rows = con.execute("""
        SELECT series_id, COALESCE(short_name, name), section, frequency, units, measure,
               COALESCE(next_release_at, next_release), last_release_at, ons_release,
               last_release_url, news_query
        FROM series
    """).fetchall()
    keys = ["series_id", "name", "section", "frequency", "units", "measure",
            "next_release", "last_release", "ons_release", "page_url", "news_query"]
    info = {r[0]: dict(zip(keys, r)) for r in rows}
    # Short names carry units for chart legends, e.g. "CPI inflation (%)"; the email shows units on the values
    for s in info.values():
        s["name"] = s["name"].replace(" (%)", "")
    return info


def latest_two(con, series_id):
    return con.execute("""
        SELECT period_date, value FROM latest_observations
        WHERE series_id = ? ORDER BY period_date DESC LIMIT 2
    """, [series_id]).fetchall()


def value_on_or_before(con, series_id, day):
    row = con.execute("""
        SELECT value FROM latest_observations
        WHERE series_id = ? AND period_date <= ?
        ORDER BY period_date DESC LIMIT 1
    """, [series_id, day]).fetchone()
    return row[0] if row else None


def yoy_change(con, series_id, period):
    row = con.execute("SELECT change_yoy FROM changes WHERE series_id = ? AND period_date = ?",
                      [series_id, period]).fetchone()
    return row[0] if row else None


BASIS_LABEL = {"mom": "m/m", "qoq": "q/q", "yoy": "y/y"}
BASIS_PREFERENCE = ["value", "mom", "qoq", "yoy"]  # which consensus to show when several exist


def consensus_for(con, series_id, period, captured_before=None):
    """The latest consensus captured for one series and period, as (basis, value),
    preferring the headline basis. captured_before limits it to figures captured
    before a date (so a surprise compares against the pre-release consensus)."""
    rows = con.execute("""
        SELECT basis, value FROM consensus
        WHERE series_id = ? AND period_date = ? AND (? IS NULL OR as_of <= ?)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY basis ORDER BY as_of DESC) = 1
    """, [series_id, period, captured_before, captured_before]).fetchall()
    rows.sort(key=lambda r: BASIS_PREFERENCE.index(r[0]))
    return rows[0] if rows else None


def actual_on_basis(con, series_id, period, basis):
    """The published figure on the same basis as a consensus: the value itself,
    or the period / year-on-year % change from the changes view."""
    if basis == "value":
        row = con.execute("SELECT value FROM latest_observations WHERE series_id = ? AND period_date = ?",
                          [series_id, period]).fetchone()
    else:
        column = "change_yoy" if basis == "yoy" else "change_period"
        row = con.execute(f"SELECT {column} FROM changes WHERE series_id = ? AND period_date = ?",
                          [series_id, period]).fetchone()
    return row[0] if row else None


def fmt_consensus(value, basis, s):
    if basis == "value":
        return _fmt(value, s["units"], s["frequency"])
    return f"{value:+.1f}% {BASIS_LABEL[basis]}"


def fmt_surprise(diff, basis, s):
    """Actual minus consensus: pp for rates and growth rates, bp for daily rates, units otherwise."""
    if diff is None:
        return "-"
    if basis != "value" or s["units"] == "%":
        return f"{diff * 100:+.0f}bp" if s["frequency"] == "D" else f"{diff:+.1f}pp"
    return f"{diff:+,.0f}" if s["units"] in ("£m", "thousands", "number") else f"{diff:+.1f}"


def news_url(query):
    """Google News search for the past week (the when:7d operator), UK edition."""
    if not query:
        return None
    return "https://news.google.com/search?" + urlencode(
        {"q": f"{query} when:7d", "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})


def fmt_when(moment):
    """'Wed 18 Nov 07:00' for a release time, 'Wed 18 Nov' for a date."""
    if moment is None:
        return "-"
    return f"{moment:%a %d %b %H:%M}" if isinstance(moment, datetime) else f"{moment:%a %d %b}"


def history(con, series_id, since):
    return con.execute("""
        SELECT period_date, value FROM latest_observations
        WHERE series_id = ? AND period_date >= ? ORDER BY period_date
    """, [series_id, since]).fetchall()


def previous_release_history(con, series_id, since):
    """The series as it stood at the previous release: for each period, the latest
    value from a vintage before the most recent one. Empty until a series has
    been through at least two releases since the pipeline started."""
    return con.execute("""
        WITH latest_vintage AS (
            SELECT max(vintage_date) AS v FROM observations WHERE series_id = ?
        )
        SELECT period_date, value
        FROM observations, latest_vintage
        WHERE series_id = ? AND period_date >= ? AND vintage_date < latest_vintage.v
        QUALIFY ROW_NUMBER() OVER (PARTITION BY period_date ORDER BY vintage_date DESC) = 1
        ORDER BY period_date
    """, [series_id, series_id, since]).fetchall()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_change(value, measure, frequency):
    """+2.1% for levels, +0.3pp for rates (+5bp for daily rates), +845 for balances."""
    if value is None:
        return "-"
    if measure == "level":
        return f"{value:+.1f}%"
    if measure == "rate":
        return f"{value * 100:+.0f}bp" if frequency == "D" else f"{value:+.1f}pp"
    return f"{value:+,.0f}"


def change_between(new, old, measure):
    if new is None or old is None:
        return None
    if measure == "level":
        return (new / old - 1) * 100 if old else None
    return new - old


SPARK_W, SPARK_H = 200, 50  # pixels


def sparkline(points, previous=()):
    """A small trend line as PNG bytes: the shape matters, not the axis values.
    previous (the series at the previous release) is drawn first as a red dotted
    line, so it is hidden under the blue line except where data were revised."""
    if len(points) < 2:
        return None
    fig, ax = plt.subplots(figsize=(SPARK_W / 100, SPARK_H / 100), dpi=100)
    if len(previous) >= 2:
        ax.plot([p[0] for p in previous], [p[1] for p in previous],
                color=COLOURS["down"], linewidth=1.3, linestyle=(0, (1.5, 1.5)), zorder=1)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ax.plot(xs, ys, color=COLOURS["line"], linewidth=1.2, zorder=2)
    ax.plot(xs[-1], ys[-1], "o", color=COLOURS["line"], markersize=2.5, zorder=3)
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Building the report
# ---------------------------------------------------------------------------

def build_report(con, results, run_started, today=None, consensus_captured=(), consensus_notes=(),
                 extra_problems=()):
    """Collect everything the email needs. Returns a dict of sections and images."""
    today = today or date.today()
    info = series_info(con)
    rows = changes_this_run(con, run_started)

    new_periods = {}   # series_id -> count of new periods
    revisions = {}     # series_id -> list of (period, old, new)
    for sid, period, new, old in rows:
        if old is None:
            new_periods[sid] = new_periods.get(sid, 0) + 1
        else:
            revisions.setdefault(sid, []).append((period, old, new))

    images = {}  # cid key -> png bytes, one sparkline per series, reused across sections

    revised = set()  # series whose previous release differs from the latest in the charted window

    def add_spark(sid, freq):
        if sid not in images:
            since = today - timedelta(days=365 if freq == "D" else 3 * 365)
            latest, previous = history(con, sid, since), previous_release_history(con, sid, since)
            latest_by_period = dict(latest)
            if any(abs(latest_by_period[p] - v) > 1e-9 for p, v in previous if p in latest_by_period):
                revised.add(sid)
            png = sparkline(latest, previous)
            if not png:
                return None
            images[sid] = png
        return sid

    def release_row(sid):
        """Latest position of a monthly or quarterly series."""
        s = info[sid]
        two = latest_two(con, sid)
        if not two:
            return None
        period, value = two[0]
        previous = two[1][1] if len(two) > 1 else None
        # Consensus captured before the release, compared with the outturn on the same basis
        released_on = s["last_release"].date() if s["last_release"] else None
        cons = consensus_for(con, sid, period, captured_before=released_on)
        consensus_text, surprise_text = "-", "-"
        if cons:
            basis, cons_value = cons
            actual = actual_on_basis(con, sid, period, basis)
            consensus_text = fmt_consensus(cons_value, basis, s)
            surprise_text = fmt_surprise(actual - cons_value if actual is not None else None, basis, s)
        return dict(
            sid=sid, name=s["name"], section=s["section"], period=_fmt_period(period, s["frequency"]),
            value=_fmt(value, s["units"], s["frequency"]),
            previous=_fmt(previous, s["units"], s["frequency"]),
            yoy=fmt_change(yoy_change(con, sid, period), s["measure"], s["frequency"]),
            released=fmt_when(s["last_release"]), next_release=fmt_when(s["next_release"]),
            consensus=consensus_text, surprise=surprise_text,
            page_url=s["page_url"], news_url=news_url(s["news_query"]),
            is_new=sid in new_periods, spark=add_spark(sid, s["frequency"]), is_revised=sid in revised)

    def market_row(sid):
        """Latest position of a daily series, with 1-day and 1-month changes."""
        s = info[sid]
        two = latest_two(con, sid)
        if not two:
            return None
        period, value = two[0]
        previous = two[1][1] if len(two) > 1 else None
        month_ago = value_on_or_before(con, sid, period - timedelta(days=30))
        return dict(
            sid=sid, name=s["name"], section=s["section"], period=_fmt_period(period, "D"),
            value=_fmt(value, s["units"], "D"),
            change_1d=fmt_change(change_between(value, previous, s["measure"]), s["measure"], "D"),
            change_1m=fmt_change(change_between(value, month_ago, s["measure"]), s["measure"], "D"),
            page_url=s["page_url"], news_url=news_url(s["news_query"]),
            spark=add_spark(sid, "D"))

    daily = [sid for sid, s in info.items() if s["frequency"] == "D"]
    periodic = [sid for sid, s in info.items() if s["frequency"] != "D"]
    # New releases: monthly/quarterly series with a new period this run
    releases = [r for r in (release_row(sid) for sid in periodic if sid in new_periods) if r]
    # Markets and the full data section always show every series, updated today or not
    markets = [r for r in (market_row(sid) for sid in daily) if r]
    all_data = [r for r in (release_row(sid) for sid in periodic) if r]

    revision_lines, older_revisions = [], 0
    for sid, revs in revisions.items():
        s = info[sid]
        latest_period = latest_two(con, sid)[0][0]
        months = REVISION_WINDOW_MONTHS[s["frequency"]]
        cutoff = date(latest_period.year, latest_period.month, 1) - timedelta(days=31 * months)
        for period, old, new in revs:
            if period >= cutoff:
                revision_lines.append(dict(
                    name=s["name"], period=_fmt_period(period, s["frequency"]),
                    old=_fmt(old, s["units"], s["frequency"]), new=_fmt(new, s["units"], s["frequency"])))
            else:
                older_revisions += 1

    # Coming up: one entry per release in the next 7 days, with any consensus
    # captured since the series was last published
    window_end = today + timedelta(days=7)
    releases_due = {}
    for sid, s in info.items():
        when = s["next_release"]
        if when is None:
            continue
        when_date = when.date() if isinstance(when, datetime) else when
        if not today <= when_date <= window_end:
            continue
        release = (s["ons_release"] or s["name"]).split("|")[0]
        entry = releases_due.setdefault((when, release), [])
        latest = latest_two(con, sid)
        latest_period = latest[0][0] if latest else date.min
        row = con.execute("""
            SELECT basis, value, period_date FROM consensus
            WHERE series_id = ? AND (period_date > ? OR as_of > ?)
            ORDER BY period_date DESC, as_of DESC LIMIT 1
        """, [sid, latest_period, s["last_release"].date() if s["last_release"] else date.min]).fetchone()
        if row:
            entry.append(f'{s["name"]} {fmt_consensus(row[1], row[0], s)} '
                         f'({_fmt_period(row[2], s["frequency"])})')
    # Bank Rate and other daily series: consensus for a future date (e.g. an MPC decision)
    for sid, s in info.items():
        if s["frequency"] != "D":
            continue
        for basis, value, period in con.execute("""
            SELECT basis, value, period_date FROM consensus
            WHERE series_id = ? AND period_date BETWEEN ? AND ?
            QUALIFY ROW_NUMBER() OVER (PARTITION BY period_date ORDER BY as_of DESC) = 1
        """, [sid, today, window_end]).fetchall():
            releases_due.setdefault((period, f'{s["name"]} decision'), []).append(
                f'{s["name"]} {fmt_consensus(value, basis, s)}')
    coming_up = [dict(when=fmt_when(when), release=release, consensus=items)
                 for (when, release), items in sorted(releases_due.items(),
                                                     key=lambda kv: (kv[0][0] if isinstance(kv[0][0], datetime)
                                                                     else datetime.combine(kv[0][0], datetime.min.time()),
                                                                     kv[0][1]))]

    problems = [dict(name=info.get(r["series_id"], {}).get("name", r["series_id"]),
                     detail=r["message"]) for r in results if r["status"] == "error"]
    problems += [dict(name=name, detail=f"health: {health}")
                 for name, health in con.execute(
                     "SELECT name, health FROM data_health WHERE health NOT IN ('ok', 'error')").fetchall()]

    # Within a section, follow the order of series.csv (e.g. Bank Rate, SONIA, then 5y, 10y, 20y gilts)
    position = {row["series_id"]: i for i, row in enumerate(load_registry())}
    section_order = ["Activity", "Labour market", "Prices", "Households", "Public finances", "External",
                     "Monetary policy and rates", "Money and credit"]
    market_order = ["Monetary policy and rates", "External"]  # rates first, then sterling

    def sort_key(order):
        return lambda r: (order.index(r["section"]) if r["section"] in order else 99, position.get(r["sid"], 999))

    releases.sort(key=sort_key(section_order))
    markets.sort(key=sort_key(market_order))
    all_data.sort(key=sort_key(section_order))

    problems += [dict(name="Pipeline", detail=p) for p in extra_problems]
    captured = [dict(name=info[c["series_id"]]["name"],
                     period=_fmt_period(c["period_date"], info[c["series_id"]]["frequency"]),
                     consensus=fmt_consensus(c["value"], c["basis"], info[c["series_id"]]),
                     event=c["event"], file=c["file"])
                for c in consensus_captured]

    return dict(today=today, releases=releases, markets=markets, all_data=all_data,
                revisions=revision_lines, older_revisions=older_revisions,
                coming_up=coming_up, captured=captured, notes=list(consensus_notes),
                problems=problems, images=images)


def subject_line(report):
    parts = []
    n = len(report["releases"])
    parts.append(f"{n} new release{'s' if n != 1 else ''}" if n else "no new releases")
    if report["revisions"]:
        parts.append(f"{len(report['revisions'])} revision{'s' if len(report['revisions']) != 1 else ''}")
    if report["problems"]:
        parts.append(f"{len(report['problems'])} problem{'s' if len(report['problems']) != 1 else ''}")
    return f"UK macro monitor, {report['today']:%a %d %b %Y}: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Rendering (HTML for the email, plain text as a fallback)
# ---------------------------------------------------------------------------

def _table(headers, rows, align, widths=None):
    """An HTML table with inline styles, since many email clients ignore <style> blocks.
    widths (percentages) fix the column layout, so separate tables line up."""
    c = COLOURS
    widths = widths or [None] * len(headers)
    th = "".join(f'<th style="text-align:{a};padding:6px 10px;border-bottom:1px solid {c["rule"]};'
                 f'background:{c["head"]};font-weight:600;font-size:12px;color:{c["muted"]}'
                 f'{f";width:{w}%" if w else ""}">{h}</th>'
                 for h, a, w in zip(headers, align, widths))
    body = ""
    for row in rows:
        tds = "".join(f'<td style="text-align:{a};padding:6px 10px;border-bottom:1px solid {c["rule"]};'
                      f'font-size:13px;white-space:nowrap">{cell}</td>' for cell, a in zip(row, align))
        body += f"<tr>{tds}</tr>"
    layout = "table-layout:fixed;" if any(widths) else ""
    return (f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;{layout}'
            f'margin:0 0 20px 0">{"<tr>" + th + "</tr>"}{body}</table>')


# Changes are shown in a neutral colour: whether a rise is good news depends on the
# series (higher GDP versus higher unemployment), so red/green would mislead.
def _coloured(text):
    return f'<span style="color:{COLOURS["muted"]}">{html.escape(text)}</span>'


def render_html(report, image_src):
    """image_src(cid_key) returns the src for an image: 'cid:...' in the email,
    a data: URI in the browser preview."""
    c = COLOURS
    esc = html.escape

    def img(key):
        return f'<img src="{image_src(key)}" width="{SPARK_W}" height="{SPARK_H}" alt="">' if key else ""

    def h2(text):
        return (f'<h2 style="font-size:15px;margin:24px 0 8px 0;color:{c["text"]};'
                f'border-bottom:2px solid {c["text"]};padding-bottom:4px">{text}</h2>')

    def h3(text):
        return f'<h3 style="font-size:13px;margin:14px 0 4px 0;color:{c["muted"]}">{text}</h3>'

    def new_badge(r):
        return (f' <span style="font-size:10px;font-weight:600;color:#fff;background:{c["line"]};'
                f'border-radius:3px;padding:1px 4px">NEW</span>') if r.get("is_new") else ""

    def revised_badge(r):
        return (f' <span style="font-size:10px;font-weight:600;color:#fff;background:{c["down"]};'
                f'border-radius:3px;padding:1px 4px">REVISED</span>') if r.get("is_revised") else ""

    def small(text):
        return f'<span style="font-size:12px;color:{c["muted"]}">{text}</span>'

    def name_cell(r):
        """The series name, linked to its latest ONS release page where there is one."""
        if r.get("page_url"):
            return (f'<a href="{esc(r["page_url"])}" style="color:{c["text"]};text-decoration:underline;'
                    f'text-decoration-color:{c["rule"]}">{esc(r["name"])}</a>')
        return esc(r["name"])

    def wrap(content):
        """Table cells do not wrap (numbers stay on one line); names and tags may."""
        return f'<span style="white-space:normal">{content}</span>'

    def news_button(r):
        """A button-style link: Google News for the series over the past week."""
        if not r.get("news_url"):
            return ""
        return (f'<a href="{esc(r["news_url"])}" style="display:inline-block;padding:3px 9px;font-size:11px;'
                f'font-weight:600;color:{c["line"]};border:1px solid {c["line"]};border-radius:4px;'
                f'text-decoration:none;white-space:nowrap">News</a>')

    # New releases: the outturn against consensus, and when it came out
    new_headers = ["Series", "Period", "Latest", "Previous", "Consensus", "Surprise", "vs year ago",
                   "Last 3 years", "Released", ""]
    new_align = ["left", "left", "right", "right", "right", "right", "right", "left", "left", "right"]

    def new_rows(rows):
        return [[wrap(name_cell(r) + revised_badge(r)), r["period"], f'<b>{r["value"]}</b>', r["previous"], r["consensus"],
                 f'<b>{esc(r["surprise"])}</b>' if r["surprise"] != "-" else "-", _coloured(r["yoy"]),
                 img(r["spark"]), small(r["released"]), news_button(r)] for r in rows]

    # All UK data: the latest position of every series, with last and next release times
    all_headers = ["Series", "Period", "Latest", "Previous", "vs year ago", "Last 3 years", "Released",
                   "Next release", ""]
    all_align = ["left", "left", "right", "right", "right", "left", "left", "left", "right"]
    all_widths = [20, 7, 8, 8, 8, 23, 11, 11, 4]  # same in every section, so the columns line up

    def all_rows(rows):
        return [[wrap(name_cell(r) + new_badge(r) + revised_badge(r)), r["period"], f'<b>{r["value"]}</b>', r["previous"],
                 _coloured(r["yoy"]), img(r["spark"]), small(r["released"]), small(r["next_release"]),
                 news_button(r)]
                for r in rows]

    legend = (f'<span style="color:{c["line"]};font-weight:600">blue</span> = latest release, '
              f'<span style="color:{c["down"]};font-weight:600">red dotted</span> = previous release '
              f'(visible where data were revised; REVISED tags the series)')
    parts = [f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:{c["text"]};max-width:1040px">',
             f'<h1 style="font-size:20px;margin:0 0 4px 0">UK macro monitor</h1>',
             f'<div style="color:{c["muted"]};font-size:13px">{report["today"]:%A %d %B %Y}. '
             f'Release times are UK time. Consensus: <a href="{CALENDAR_URL}">Bloomberg economic calendar</a>. '
             f'Series names link to the ONS release page.<br>Trend lines: {legend}.</div>']

    # 1. New releases
    parts.append(h2("New releases"))
    if report["releases"]:
        parts.append(_table(new_headers, new_rows(report["releases"]), new_align))
    else:
        parts.append(f'<p style="font-size:13px;color:{c["muted"]}">No new monthly or quarterly releases.</p>')

    # 2. Coming up, with any consensus captured for each release
    parts.append(h2("Coming up (next 7 days)"))
    if report["coming_up"]:
        parts.append(_table(["When", "Release", "Consensus"],
                            [[f'<b>{esc(r["when"])}</b>', esc(r["release"]),
                              "<br>".join(esc(x) for x in r["consensus"]) or small("none captured yet")]
                             for r in report["coming_up"]],
                            ["left", "left", "left"]))
    else:
        parts.append(f'<p style="font-size:13px;color:{c["muted"]}">No scheduled releases.</p>')

    # What was read from screenshots this run, so misreads are easy to spot
    if report["captured"] or report["notes"]:
        parts.append(h2("Consensus captured from screenshots"))
        if report["captured"]:
            parts.append(_table(["Series", "Period", "Consensus", "As shown on the calendar", "Screenshot"],
                                [[esc(x["name"]), x["period"], f'<b>{esc(x["consensus"])}</b>',
                                  esc(x["event"]), small(esc(x["file"]))] for x in report["captured"]],
                                ["left", "left", "right", "left", "left"]))
        for note in report["notes"]:
            parts.append(f'<p style="font-size:12px;color:{c["muted"]}">Note: {esc(note)}</p>')

    # 3. Markets (every daily series)
    if report["markets"]:
        parts.append(h2("Markets"))
        parts.append(_table(
            ["Series", "Date", "Latest", "1 day", "1 month", "Last 12 months", ""],
            [[name_cell(r), r["period"], f'<b>{r["value"]}</b>', _coloured(r["change_1d"]),
              _coloured(r["change_1m"]), img(r["spark"]), news_button(r)] for r in report["markets"]],
            ["left", "left", "right", "right", "right", "left", "right"]))

    # 4. Every monthly and quarterly series, by section
    if report["all_data"]:
        parts.append(h2("All UK data"))
        sections = []
        for r in report["all_data"]:
            if r["section"] not in sections:
                sections.append(r["section"])
        for section in sections:
            parts.append(h3(esc(section)))
            parts.append(_table(all_headers, all_rows([r for r in report["all_data"] if r["section"] == section]),
                                all_align, all_widths))

    # 5. Revisions and problems
    if report["revisions"] or report["older_revisions"]:
        parts.append(h2("Revisions"))
        if report["revisions"]:
            parts.append(_table(["Series", "Period", "Was", "Now"],
                                [[esc(r["name"]), r["period"], r["old"], f'<b>{r["new"]}</b>']
                                 for r in report["revisions"]],
                                ["left", "left", "right", "right"]))
        if report["older_revisions"]:
            parts.append(f'<p style="font-size:12px;color:{c["muted"]}">Plus {report["older_revisions"]:,} '
                         f'revisions to older periods (typically seasonal adjustment).</p>')

    if report["problems"]:
        parts.append(h2("Problems"))
        parts.append(_table(["Series", "Detail"],
                            [[esc(p["name"]), f'<span style="color:{c["down"]}">{esc(p["detail"])}</span>']
                             for p in report["problems"]], ["left", "left"]))

    parts.append(f'<p style="font-size:11px;color:{c["muted"]};margin-top:28px">Sources: ONS, Bank of England. '
                 f'Generated by the forecasting_and_modelling pipeline.</p></div>')
    return "".join(parts)


def render_text(report):
    lines = [f"UK macro monitor, {report['today']:%A %d %B %Y}", ""]
    lines.append("NEW RELEASES")
    for r in report["releases"] or []:
        lines.append(f"  {r['name']}: {r['value']} in {r['period']} (previous {r['previous']}, "
                     f"consensus {r['consensus']}, surprise {r['surprise']}, vs year ago {r['yoy']}; "
                     f"released {r['released']})")
    if not report["releases"]:
        lines.append("  None")
    lines += ["", "COMING UP (UK time)"]
    for r in report["coming_up"]:
        cons = "; ".join(r["consensus"]) or "no consensus captured yet"
        lines.append(f"  {r['when']}: {r['release']} ({cons})")
    if not report["coming_up"]:
        lines.append("  None")
    if report["captured"]:
        lines += ["", "CONSENSUS CAPTURED FROM SCREENSHOTS"]
        for x in report["captured"]:
            lines.append(f"  {x['name']} {x['period']}: {x['consensus']} (shown as '{x['event']}')")
    if report["markets"]:
        lines += ["", "MARKETS"]
        for r in report["markets"]:
            lines.append(f"  {r['name']}: {r['value']} on {r['period']} (1 day {r['change_1d']}, 1 month {r['change_1m']})")
    if report["all_data"]:
        lines += ["", "ALL UK DATA"]
        section = None
        for r in report["all_data"]:
            if r["section"] != section:
                section = r["section"]
                lines.append(f"  {section}")
            tag = " [NEW]" if r["is_new"] else ""
            lines.append(f"    {r['name']}{tag}: {r['value']} in {r['period']} (previous {r['previous']}, "
                         f"vs year ago {r['yoy']}; released {r['released']}, next {r['next_release']})")
    if report["revisions"]:
        lines += ["", "REVISIONS"]
        for r in report["revisions"]:
            lines.append(f"  {r['name']} {r['period']}: {r['old']} -> {r['new']}")
    if report["problems"]:
        lines += ["", "PROBLEMS"]
        for p in report["problems"]:
            lines.append(f"  {p['name']}: {p['detail']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_preview(report):
    """Write a browser-viewable copy, with images embedded as data URIs."""
    def data_uri(key):
        return "data:image/png;base64," + base64.b64encode(report["images"][key]).decode()
    PREVIEW_PATH.parent.mkdir(exist_ok=True)
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(subject_line(report))}</title>"
            f"</head><body style='margin:24px;background:#fff'>{render_html(report, data_uri)}</body></html>")
    PREVIEW_PATH.write_text(page, encoding="utf-8")
    return PREVIEW_PATH


def build_message(report, sender, recipient):
    """A multipart email: plain text, plus HTML with the charts attached inline.
    Inline images are referenced by Content-ID (cid:) so Gmail shows them in the body."""
    msg = EmailMessage()
    msg["Subject"] = subject_line(report)
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(render_text(report))

    cids = {key: make_msgid(domain="macro-monitor") for key in report["images"]}
    msg.add_alternative(render_html(report, lambda key: "cid:" + cids[key][1:-1]), subtype="html")
    html_part = msg.get_payload()[1]
    for key, png in report["images"].items():
        html_part.add_related(png, maintype="image", subtype="png", cid=cids[key])
    return msg


def send_report(report):
    """Send via the Gmail API. Returns a short status message; never raises, so a
    mail problem cannot break the data run."""
    load_dotenv(ROOT / ".env")
    sender = os.getenv("GMAIL_ADDRESS")
    recipient = os.getenv("REPORT_TO") or sender
    if not sender:
        return "Email not sent: GMAIL_ADDRESS is not set in .env"
    try:
        gmail.send(build_message(report, sender, recipient))
        return f"Email sent to {recipient}"
    except gmail.GmailNotAuthorised as e:
        return f"Email not sent: {e}"
    except Exception as e:
        return f"Email not sent: {type(e).__name__}: {e}"