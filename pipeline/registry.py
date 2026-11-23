"""The series registry: series.csv is the list of everything the monitor tracks.

The CSV is the source of truth. Each run copies it into the `series` table so
SQL views and Power BI can join data to names, units and sections.
"""
import csv

from pipeline.db import ROOT

REGISTRY_PATH = ROOT / "series.csv"

COLUMNS = ["series_id", "source", "source_code", "dataset", "name", "short_name",
           "section", "area", "frequency", "units", "measure"]
FREQUENCIES = {"D", "M", "Q", "A"}
# rate: a percentage (changes in percentage points)
# level: an index or stock that is always positive (changes in %)
# balance: a flow that can be negative, e.g. borrowing or a trade balance (changes in its own units)
MEASURES = {"rate", "level", "balance"}


def load_registry(path=REGISTRY_PATH):
    """Read series.csv and check it is valid. Returns a list of dicts."""
    # utf-8-sig copes with the invisible marker Excel adds when saving "CSV UTF-8"
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    problems = []
    seen = set()
    for line_no, row in enumerate(rows, start=2):  # line 1 is the header
        for col in COLUMNS:
            row[col] = (row.get(col) or "").strip()
            if not row[col]:
                problems.append(f"line {line_no}: missing {col}")
        if row["series_id"] in seen:
            problems.append(f"line {line_no}: duplicate series_id {row['series_id']}")
        seen.add(row["series_id"])
        if row["frequency"] not in FREQUENCIES:
            problems.append(f"line {line_no}: frequency must be one of {sorted(FREQUENCIES)}")
        if row["measure"] not in MEASURES:
            problems.append(f"line {line_no}: measure must be one of {sorted(MEASURES)}")

        # Optional: the ONS release(s) that publish the series, "|"-separated
        row["ons_release"] = (row.get("ons_release") or "").strip()

        # Optional: multiply source values, e.g. -1 to flip a sign convention.
        try:
            row["multiplier"] = float(row.get("multiplier") or 1)
        except ValueError:
            problems.append(f"line {line_no}: multiplier must be a number")

    if problems:
        raise ValueError("series.csv has problems:\n  " + "\n  ".join(problems))
    return rows


def sync_registry(con, rows):
    """Insert new series and update details of existing ones.

    ON CONFLICT ... DO UPDATE is an "upsert": insert the row, or if that
    series_id already exists, update it instead. `excluded` refers to the row we
    tried to insert. next_release and last_fetched are left alone because the
    pipeline, not the CSV, maintains them.
    """
    for r in rows:
        con.execute("""
            INSERT INTO series (series_id, source, source_code, dataset, name, short_name,
                                section, area, frequency, units, measure, ons_release)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (series_id) DO UPDATE SET
                ons_release = excluded.ons_release,
                source = excluded.source,
                source_code = excluded.source_code,
                dataset = excluded.dataset,
                name = excluded.name,
                short_name = excluded.short_name,
                section = excluded.section,
                area = excluded.area,
                frequency = excluded.frequency,
                units = excluded.units,
                measure = excluded.measure
        """, [r[c] for c in COLUMNS] + [r["ons_release"] or None])
