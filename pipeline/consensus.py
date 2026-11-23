"""Market consensus figures for upcoming releases.

Consensus figures live in consensus_inbox/consensus.csv, one row per figure:

    series_id,period,basis,consensus,as_of,event_as_shown,source
    cpi_yoy,2026-11,value,3.4,2026-12-14,CPI YoY (Nov),Bloomberg

  series_id       a series from series.csv
  period          reference period: YYYY-MM (monthly), YYYY-Qn (quarterly), YYYY-MM-DD (daily)
  basis           value (the series itself) or mom / qoq / yoy (% change of a level series)
  consensus       the figure, in the series' units (e.g. GBP millions, not billions)
  as_of           date the consensus was captured (YYYY-MM-DD)
  event_as_shown  the event name as written on the calendar (for checking)
  source          e.g. Bloomberg

Rows are usually added by the /consensus command in Claude Code, which reads
screenshots of the Bloomberg economic calendar, but they can be typed by hand.
Every run validates each row against the series registry and loads it into the
consensus table. The folder is gitignored: the figures are the calendar
provider's data, for personal use only.
"""
import csv
from datetime import date, datetime

from pipeline.db import ROOT

INBOX = ROOT / "consensus_inbox"
CSV_PATH = INBOX / "consensus.csv"
FIELDS = ["series_id", "period", "basis", "consensus", "as_of", "event_as_shown", "source"]
BASES = {"value", "mom", "qoq", "yoy"}


def period_date(period, frequency):
    """'2026-11' -> 2026-11-01, '2026-Q4' -> 2026-10-01, '2026-12-17' -> itself."""
    if frequency == "Q":
        year, q = period.upper().split("-Q")
        return date(int(year), 3 * (int(q) - 1) + 1, 1)
    if frequency == "D":
        return date.fromisoformat(period)
    year, month = period.split("-")[:2]
    return date(int(year), int(month), 1)


def validate(row, series):
    """Return (clean values, None) if the row fits the registry, else (None, reason)."""
    sid = (row.get("series_id") or "").strip()
    s = series.get(sid)
    if not s:
        return None, f"unknown series_id '{sid}'"
    basis = (row.get("basis") or "").strip()
    if basis not in BASES:
        return None, f"basis must be one of {sorted(BASES)}"
    if s["measure"] in ("rate", "balance") and basis != "value":
        return None, f"{sid} is a {s['measure']}, so basis must be 'value'"
    if s["frequency"] == "Q" and basis == "mom":
        return None, f"{sid} is quarterly, so basis cannot be 'mom'"
    try:
        period = period_date(row["period"].strip(), s["frequency"])
        value = float(row["consensus"])
        as_of = date.fromisoformat(row["as_of"].strip())
    except (KeyError, ValueError, IndexError, AttributeError):
        return None, "could not read period, consensus or as_of"
    return dict(series_id=sid, period_date=period, basis=basis, value=value, as_of=as_of,
                source=(row.get("source") or "").strip() or "manual",
                event=(row.get("event_as_shown") or "").strip()), None


def load_consensus(con):
    """Load consensus.csv into the consensus table.
    Returns (rows new since the last run, problems)."""
    if not CSV_PATH.exists():
        return [], []
    series = {sid: {"frequency": f, "measure": m}
              for sid, f, m in con.execute("SELECT series_id, frequency, measure FROM series").fetchall()}

    captured, problems = [], []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            clean, reason = validate(row, series)
            if reason:
                problems.append(f"consensus.csv line {line_no}: {reason}")
                continue
            already = con.execute("""
                SELECT value FROM consensus
                WHERE series_id = ? AND period_date = ? AND basis = ? AND as_of = ?
            """, [clean["series_id"], clean["period_date"], clean["basis"], clean["as_of"]]).fetchone()
            if already is not None and already[0] == clean["value"]:
                continue  # loaded on an earlier run
            con.execute("INSERT OR REPLACE INTO consensus VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [clean["series_id"], clean["period_date"], clean["basis"], clean["value"],
                         clean["as_of"], clean["source"], clean["event"], CSV_PATH.name, datetime.now()])
            captured.append(dict(series_id=clean["series_id"], period_date=clean["period_date"],
                                 basis=clean["basis"], value=clean["value"], event=clean["event"],
                                 file=CSV_PATH.name))
    return captured, problems
