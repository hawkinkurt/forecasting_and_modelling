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

Sending uses Gmail's SMTP server with an app password from .env. Every run also
saves an HTML preview to exports/last_report.html.
"""
import base64
import html
import io
import os
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import make_msgid

import matplotlib

matplotlib.use("Agg")  # draw to images without opening windows
import matplotlib.pyplot as plt  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from pipeline.db import ROOT  # noqa: E402
from pipeline.registry import load_registry  # noqa: E402
from pipeline.report import _fmt, _fmt_period  # noqa: E402

PREVIEW_PATH = ROOT / "exports" / "last_report.html"

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
        SELECT series_id, COALESCE(short_name, name), section, frequency, units, measure, next_release
        FROM series
    """).fetchall()
    info = {r[0]: dict(zip(["series_id", "name", "section", "frequency", "units", "measure", "next_release"], r))
            for r in rows}
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


def history(con, series_id, since):
    return con.execute("""
        SELECT period_date, value FROM latest_observations
        WHERE series_id = ? AND period_date >= ? ORDER BY period_date
    """, [series_id, since]).fetchall()


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


def sparkline(points):
    """A small trend line as PNG bytes: the shape matters, not the axis values."""
    if len(points) < 2:
        return None
    fig, ax = plt.subplots(figsize=(1.6, 0.4), dpi=100)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ax.plot(xs, ys, color=COLOURS["line"], linewidth=1.2)
    ax.plot(xs[-1], ys[-1], "o", color=COLOURS["line"], markersize=2.5)
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Building the report
# ---------------------------------------------------------------------------

def build_report(con, results, run_started, today=None):
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

    images = {}  # cid key -> png bytes

    def add_spark(sid, freq):
        since = today - timedelta(days=365 if freq == "D" else 5 * 365)
        png = sparkline(history(con, sid, since))
        if png:
            images[sid] = png
            return sid
        return None

    releases, markets = [], []
    for sid in new_periods:
        s = info[sid]
        two = latest_two(con, sid)
        if not two:
            continue
        period, value = two[0]
        previous = two[1][1] if len(two) > 1 else None
        if s["frequency"] == "D":
            month_ago = value_on_or_before(con, sid, period - timedelta(days=30))
            markets.append(dict(
                sid=sid, name=s["name"], section=s["section"], period=_fmt_period(period, "D"),
                value=_fmt(value, s["units"], "D"),
                change_1d=fmt_change(change_between(value, previous, s["measure"]), s["measure"], "D"),
                change_1m=fmt_change(change_between(value, month_ago, s["measure"]), s["measure"], "D"),
                spark=add_spark(sid, "D")))
        else:
            releases.append(dict(
                sid=sid, name=s["name"], section=s["section"], period=_fmt_period(period, s["frequency"]),
                value=_fmt(value, s["units"], s["frequency"]),
                previous=_fmt(previous, s["units"], s["frequency"]),
                yoy=fmt_change(yoy_change(con, sid, period), s["measure"], s["frequency"]),
                next_release=s["next_release"], spark=add_spark(sid, s["frequency"])))

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

    coming_up = con.execute("""
        SELECT next_release, list(name ORDER BY name)
        FROM series
        WHERE next_release BETWEEN ? AND ?
        GROUP BY next_release ORDER BY next_release
    """, [today, today + timedelta(days=7)]).fetchall()

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

    return dict(today=today, releases=releases, markets=markets, revisions=revision_lines,
                older_revisions=older_revisions, coming_up=coming_up, problems=problems, images=images)


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

def _table(headers, rows, align):
    """An HTML table with inline styles, since many email clients ignore <style> blocks."""
    c = COLOURS
    th = "".join(f'<th style="text-align:{a};padding:6px 10px;border-bottom:1px solid {c["rule"]};'
                 f'background:{c["head"]};font-weight:600;font-size:12px;color:{c["muted"]}">{h}</th>'
                 for h, a in zip(headers, align))
    body = ""
    for row in rows:
        tds = "".join(f'<td style="text-align:{a};padding:6px 10px;border-bottom:1px solid {c["rule"]};'
                      f'font-size:13px;white-space:nowrap">{cell}</td>' for cell, a in zip(row, align))
        body += f"<tr>{tds}</tr>"
    return (f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;'
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
        return f'<img src="{image_src(key)}" width="160" height="40" alt="">' if key else ""

    def h2(text):
        return (f'<h2 style="font-size:15px;margin:24px 0 8px 0;color:{c["text"]};'
                f'border-bottom:2px solid {c["text"]};padding-bottom:4px">{text}</h2>')

    parts = [f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;color:{c["text"]};max-width:820px">',
             f'<h1 style="font-size:20px;margin:0 0 4px 0">UK macro monitor</h1>',
             f'<div style="color:{c["muted"]};font-size:13px">{report["today"]:%A %d %B %Y}</div>']

    parts.append(h2("New releases"))
    if report["releases"]:
        parts.append(_table(
            ["Series", "Period", "Latest", "Previous", "vs year ago", "Last 5 years", "Next release"],
            [[esc(r["name"]), r["period"], f'<b>{r["value"]}</b>', r["previous"], _coloured(r["yoy"]),
              img(r["spark"]), f'{r["next_release"]:%d %b}' if r["next_release"] else "-"]
             for r in report["releases"]],
            ["left", "left", "right", "right", "right", "left", "left"]))
    else:
        parts.append(f'<p style="font-size:13px;color:{c["muted"]}">No new monthly or quarterly releases.</p>')

    if report["markets"]:
        parts.append(h2("Markets"))
        parts.append(_table(
            ["Series", "Date", "Latest", "1 day", "1 month", "Last 12 months"],
            [[esc(r["name"]), r["period"], f'<b>{r["value"]}</b>', _coloured(r["change_1d"]),
              _coloured(r["change_1m"]), img(r["spark"])] for r in report["markets"]],
            ["left", "left", "right", "right", "right", "left"]))

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

    parts.append(h2("Coming up (next 7 days)"))
    if report["coming_up"]:
        parts.append(_table(["Date", "Releases"],
                            [[f"{d:%a %d %b}", esc(", ".join(names))] for d, names in report["coming_up"]],
                            ["left", "left"]))
    else:
        parts.append(f'<p style="font-size:13px;color:{c["muted"]}">No scheduled releases.</p>')

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
        lines.append(f"  {r['name']}: {r['value']} in {r['period']} (previous {r['previous']}, vs year ago {r['yoy']})")
    if not report["releases"]:
        lines.append("  None")
    if report["markets"]:
        lines += ["", "MARKETS"]
        for r in report["markets"]:
            lines.append(f"  {r['name']}: {r['value']} on {r['period']} (1 day {r['change_1d']}, 1 month {r['change_1m']})")
    if report["revisions"]:
        lines += ["", "REVISIONS"]
        for r in report["revisions"]:
            lines.append(f"  {r['name']} {r['period']}: {r['old']} -> {r['new']}")
    lines += ["", "COMING UP"]
    for d, names in report["coming_up"]:
        lines.append(f"  {d:%a %d %b}: {', '.join(names)}")
    if not report["coming_up"]:
        lines.append("  None")
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
    """Send via Gmail. Returns a short status message; never raises, so a mail
    problem cannot break the data run."""
    load_dotenv(ROOT / ".env")
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("REPORT_TO") or sender
    if not (sender and password):
        return "Email not sent: GMAIL_ADDRESS and GMAIL_APP_PASSWORD are not set in .env"
    try:
        msg = build_message(report, sender, recipient)
        # Port 465 uses SSL from the start, so the password is never sent unencrypted
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password.replace(" ", ""))
            smtp.send_message(msg)
        return f"Email sent to {recipient}"
    except Exception as e:
        return f"Email not sent: {type(e).__name__}: {e}"
