"""Read market consensus figures from screenshots of an economic calendar.

Workflow: save a screenshot of the Bloomberg economic calendar (or any calendar
showing consensus/survey figures) into consensus_inbox/. The next run sends each
image to Claude, which returns the consensus figures that match our series as
structured JSON. Every figure is validated against the registry before it is
stored in the consensus table, and the image moves to consensus_inbox/processed/.

Claude is asked for JSON that follows a fixed schema (structured outputs), so
the reply can be parsed and checked like any other data source rather than
scraped out of free text.

Needs ANTHROPIC_API_KEY in .env. Screenshots and figures stay out of git: they
are the calendar provider's data, for personal use only.
"""
import base64
import json
import os
import shutil
from datetime import date, datetime
from typing import Literal

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from pipeline.db import ROOT

INBOX = ROOT / "consensus_inbox"
PROCESSED = INBOX / "processed"
MODEL = "claude-opus-5-5"
SOURCE = "Bloomberg (screenshot)"
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class ConsensusItem(BaseModel):
    series_id: str
    period: str                          # YYYY-MM, YYYY-Qn or YYYY-MM-DD
    basis: Literal["value", "mom", "qoq", "yoy"]
    consensus: float
    event_as_shown: str


class ConsensusExtraction(BaseModel):
    items: list[ConsensusItem]
    notes: str


# The JSON schema Claude's reply must follow. Written out rather than generated so
# it is easy to read and meets structured-output rules (no extra properties).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "series_id": {"type": "string"},
                    "period": {"type": "string"},
                    "basis": {"type": "string", "enum": ["value", "mom", "qoq", "yoy"]},
                    "consensus": {"type": "number"},
                    "event_as_shown": {"type": "string"},
                },
                "required": ["series_id", "period", "basis", "consensus", "event_as_shown"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["items", "notes"],
    "additionalProperties": False,
}


SYSTEM = """You extract market consensus forecasts from screenshots of an economic calendar
(typically Bloomberg's), for a UK macro monitoring pipeline.

Rules:
- Only report UK events that correspond to one of the series in the list provided. Ignore
  other countries and events that do not match.
- Use the consensus / survey / median forecast column. Never use the prior, actual or revised
  columns. If an event shows no consensus figure, leave it out.
- period is the reference period of the data (not the release date): YYYY-MM for monthly
  series, YYYY-Qn for quarterly series, YYYY-MM-DD for daily series such as Bank Rate decisions.
  Calendars usually show it in brackets, e.g. "CPI YoY (Oct)". Use the year that makes sense
  given the release dates shown.
- basis says what the figure measures:
    value: the series itself (rates such as CPI y/y, the unemployment rate, Bank Rate, or a
           level/balance such as borrowing), in the series' units
    mom / qoq / yoy: a % change of a level series (e.g. retail sales MoM, monthly GDP MoM)
  For a series whose measure is "rate", basis must be "value". For "balance", use "value".
- Convert to the series' units: e.g. borrowing shown as 17.5b in GBP becomes 17500 for a
  series in GBP millions. Percentages are plain numbers (3.5 for 3.5%).
- event_as_shown is the event name exactly as written on the calendar.
- Put anything ambiguous or skipped in notes."""


def _series_table(con):
    rows = con.execute("""
        SELECT s.series_id, COALESCE(s.short_name, s.name), s.frequency, s.units, s.measure,
               max(o.period_date) AS latest_period
        FROM series s LEFT JOIN observations o USING (series_id)
        WHERE s.area = 'UK'
        GROUP BY ALL ORDER BY s.series_id
    """).fetchall()
    lines = ["series_id | name | frequency | units | measure | latest period held"]
    lines += [" | ".join(str(x) for x in r) for r in rows]
    return "\n".join(lines), {r[0]: {"frequency": r[2], "measure": r[4]} for r in rows}


def _period_date(period, frequency):
    """'2026-11' -> 2026-11-01, '2026-Q4' -> 2026-10-01, '2026-12-17' -> itself."""
    if frequency == "Q":
        year, q = period.upper().split("-Q")
        return date(int(year), 3 * (int(q) - 1) + 1, 1)
    if frequency == "D":
        return date.fromisoformat(period)
    year, month = period.split("-")[:2]
    return date(int(year), int(month), 1)


def _validate(item, series):
    """Return (period_date, None) if the item fits our registry, else (None, reason)."""
    s = series.get(item.series_id)
    if not s:
        return None, f"unknown series_id {item.series_id}"
    if s["measure"] in ("rate", "balance") and item.basis != "value":
        return None, f"{item.series_id} is a {s['measure']}, so basis must be 'value'"
    if s["frequency"] == "Q" and item.basis == "mom":
        return None, f"{item.series_id} is quarterly, so basis cannot be 'mom'"
    try:
        return _period_date(item.period, s["frequency"]), None
    except (ValueError, IndexError):
        return None, f"could not read period '{item.period}' for {item.series_id}"


def extract(client, image_path, series_table):
    """Send one screenshot to Claude and return a validated ConsensusExtraction."""
    data = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        # If Opus declines the request, the API retries it on a fallback model in the same call
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": MEDIA_TYPES[image_path.suffix.lower()],
                                          "data": data}},
            {"type": "text", "text": f"Our series:\n{series_table}\n\nExtract the consensus figures."},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to read this screenshot")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the response was cut off before it finished")
    text = next(b.text for b in response.content if b.type == "text")
    return ConsensusExtraction.model_validate(json.loads(text))


def process_inbox(con):
    """Read every screenshot in the inbox. Returns (captured items, notes, problems)."""
    images = sorted(p for p in INBOX.glob("*") if p.suffix.lower() in MEDIA_TYPES) if INBOX.exists() else []
    if not images:
        return [], [], []

    load_dotenv(ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        return [], [], [f"{len(images)} screenshot(s) waiting in consensus_inbox, "
                         f"but ANTHROPIC_API_KEY is not set in .env"]

    client = anthropic.Anthropic()
    series_table, series = _series_table(con)
    captured, notes, problems = [], [], []
    PROCESSED.mkdir(parents=True, exist_ok=True)

    for path in images:
        as_of = datetime.fromtimestamp(path.stat().st_mtime).date()  # when the screenshot was taken
        try:
            result = extract(client, path, series_table)
        except (anthropic.APIError, ValidationError, RuntimeError, json.JSONDecodeError, StopIteration) as e:
            problems.append(f"could not read {path.name}: {type(e).__name__}: {e}")
            continue  # leave it in the inbox to retry next run

        for item in result.items:
            period_date, reason = _validate(item, series)
            if reason:
                problems.append(f"{path.name}: skipped '{item.event_as_shown}': {reason}")
                continue
            con.execute("""
                INSERT OR REPLACE INTO consensus
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [item.series_id, period_date, item.basis, item.consensus, as_of, SOURCE,
                  item.event_as_shown, path.name, datetime.now()])
            captured.append(dict(series_id=item.series_id, period_date=period_date, basis=item.basis,
                                 value=item.consensus, event=item.event_as_shown, file=path.name))
        if result.notes.strip():
            notes.append(f"{path.name}: {result.notes.strip()}")

        shutil.move(str(path), PROCESSED / f"{as_of:%Y-%m-%d}_{path.name}")

    return captured, notes, problems
