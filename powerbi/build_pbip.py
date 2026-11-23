"""Build the Power BI project (PBIP) for the UK macro monitor from code.

A .pbip project stores the report as text: the semantic model (tables,
relationships, DAX measures) in TMDL files and the report pages in JSON (PBIR).
Generating those files from this script means the dashboard is version
controlled and reproducible like the rest of the pipeline.

Usage (after run.py or export.py, because column types are read from the
Parquet exports):

    .\\.venv\\Scripts\\python.exe powerbi\\build_pbip.py

Then open powerbi\\UK Macro Monitor.pbip in Power BI Desktop and press Refresh.

This script owns the files it writes: model.tmdl, expressions.tmdl,
relationships.tmdl, tables/ and the report pages. Re-running it replaces any
edits made to those in Power BI Desktop, so make lasting changes here.
Close Power BI Desktop before running it.
"""
import csv
import json
import shutil
import uuid
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXPORTS = ROOT / "exports"
MODEL_DIR = HERE / "UK Macro Monitor.SemanticModel" / "definition"
REPORT_DIR = HERE / "UK Macro Monitor.Report" / "definition"

DASHBOARD_START = (2000, 1, 1)  # history before this stays in the database, not the dashboard

PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
PAGES_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"
VISUAL_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json"


# IDs are derived from each object's name (uuid5) rather than random, so
# re-running the script gives identical files unless something really changed,
# which keeps git diffs readable.
ID_NAMESPACE = uuid.UUID("6f1c2a9e-3b7d-4c51-9e2f-8a4d0b6c1e37")


def tag(key):
    """Power BI identifies every object by a 'lineage tag' (a GUID)."""
    return str(uuid.uuid5(ID_NAMESPACE, key))


def short_id(key):
    """Pages and visuals are named with 20 hex characters, as Desktop does."""
    return uuid.uuid5(ID_NAMESPACE, key).hex[:20]


def q(name):
    """Quote a TMDL/DAX object name if it contains anything but letters, digits or _."""
    return name if name.replace("_", "").isalnum() else "'" + name.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Semantic model: tables
# ---------------------------------------------------------------------------

# Each import table: Power BI name, Parquet file, columns to keep (None = all),
# whether to filter to the dashboard start date, extra M steps, and columns
# that should never be summed (shown row by row instead).
TABLES = [
    dict(name="Series", file="series",
         columns=["series_id", "name", "short_name", "section", "frequency", "units", "measure",
                  "source", "next_release", "last_fetched"]),
    dict(name="Observations", file="latest_observations",
         columns=["series_id", "period_date", "value", "vintage_date"], filter_dates=True, hide=True),
    dict(name="Changes", file="changes",
         columns=["series_id", "period_date", "is_complete", "change_period",
                  "change_yoy", "change_3m3m_annualised"], filter_dates=True, hide=True),
    dict(name="RealPay", file="real_pay", filter_dates=True, hide=True),
    dict(name="YieldCurve", file="yield_curve", filter_dates=True, hide=True),
    dict(name="RealPolicyRate", file="real_policy_rate", filter_dates=True, hide=True),
    dict(name="PsnbFiscalYear", file="psnb_fiscal_year", filter_dates=True,
         # Month label for the chart axis (Apr ... Mar), sorted by month_of_fy
         extra_steps=['AddMonth = Table.AddColumn(Filtered, "fy_month", each Date.ToText([period_date], "MMM"), type text)'],
         extra_columns=[("fy_month", "string")], sort_by={"fy_month": "month_of_fy"}),
    dict(name="Snapshot", file="latest_snapshot", no_summarize=True),
    dict(name="DataHealth", file="data_health", no_summarize=True),
]

DUCKDB_TO_TMDL = {"VARCHAR": "string", "DOUBLE": "double", "DATE": "dateTime",
                  "TIMESTAMP": "dateTime", "BIGINT": "int64", "INTEGER": "int64",
                  "BOOLEAN": "boolean"}


def parquet_columns(file):
    con = duckdb.connect()
    path = (EXPORTS / f"{file}.parquet").as_posix()
    return [(name, DUCKDB_TO_TMDL[dtype]) for name, dtype, *_ in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()]


def column_tmdl(table, name, dtype, is_date=False, summarize=False, hidden=False, sort_by=None):
    lines = [f"\tcolumn {q(name)}",
             f"\t\tdataType: {dtype}"]
    if hidden:
        lines.append("\t\tisHidden")
    if is_date:
        lines.append("\t\tformatString: dd mmm yyyy")
    lines += [f"\t\tlineageTag: {tag(f'column.{table}.{name}')}",
              f"\t\tsummarizeBy: {'sum' if summarize else 'none'}",
              f"\t\tsourceColumn: {name}"]
    if sort_by:
        lines.append(f"\t\tsortByColumn: {q(sort_by)}")
    lines += ["", "\t\tannotation SummarizationSetBy = Automatic"]
    if is_date:
        lines += ["", "\t\tannotation UnderlyingDateTimeDataType = Date"]
    return "\n".join(lines) + "\n"


def m_query(spec, keep):
    path = f'ExportFolder & "{spec["file"]}.parquet"'
    steps = [f"Source = Parquet.Document(File.Contents({path}), [Compression=null, LegacyColumnNameEncoding=false, MaxDepth=null])",
             f"Selected = Table.SelectColumns(Source, {{{', '.join(json.dumps(c) for c in keep)}}})"]
    last = "Selected"
    if spec.get("filter_dates"):
        # Keep the dashboard light: only periods from DashboardStart onwards
        steps.append("Filtered = Table.SelectRows(Selected, each [period_date] >= DashboardStart)")
        last = "Filtered"
    for step in spec.get("extra_steps", []):
        steps.append(step)
        last = step.split("=", 1)[0].strip()
    body = ",\n".join("\t\t\t\t    " + s for s in steps)
    return f"\t\t\t\tlet\n{body}\n\t\t\t\tin\n\t\t\t\t    {last}"


def table_tmdl(spec, measures=""):
    cols = parquet_columns(spec["file"])
    keep = [c for c, _ in cols] if spec.get("columns") is None else spec["columns"]
    types = dict(cols)
    out = [f"table {q(spec['name'])}", f"\tlineageTag: {tag('table.' + spec['name'])}", ""]
    if measures:
        out.append(measures)
    for c in keep:
        dtype = types[c]
        hidden = spec.get("hide") and c in ("series_id", "period_date", "vintage_date")
        summarize = dtype in ("double", "int64") and not spec.get("no_summarize") and c != "month_of_fy"
        out.append(column_tmdl(spec["name"], c, dtype, is_date=(dtype == "dateTime"), summarize=summarize,
                               hidden=hidden, sort_by=spec.get("sort_by", {}).get(c)))
    for c, dtype in spec.get("extra_columns", []):
        out.append(column_tmdl(spec["name"], c, dtype, sort_by=spec.get("sort_by", {}).get(c)))
    out += [f"\tpartition {q(spec['name'])} = m",
            "\t\tmode: import",
            "\t\tsource =",
            m_query(spec, keep),
            "",
            "\tannotation PBI_ResultType = Table",
            ""]
    return "\n".join(out)


DATE_TABLE = f"""table Date
\tlineageTag: {tag('table.Date')}
\tdataCategory: Time

\tcolumn Date
\t\tdataType: dateTime
\t\tisKey
\t\tformatString: dd mmm yyyy
\t\tlineageTag: {tag('column.Date.Date')}
\t\tsummarizeBy: none
\t\tisNameInferred
\t\tsourceColumn: [Date]

\t\tannotation SummarizationSetBy = Automatic

\t\tannotation UnderlyingDateTimeDataType = Date

\tcolumn Year = YEAR ( 'Date'[Date] )
\t\tdataType: int64
\t\tformatString: 0
\t\tlineageTag: {tag('column.Date.Year')}
\t\tsummarizeBy: none

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Month Start' = DATE ( YEAR ( 'Date'[Date] ), MONTH ( 'Date'[Date] ), 1 )
\t\tdataType: dateTime
\t\tformatString: mmm yyyy
\t\tlineageTag: {tag('column.Date.Month Start')}
\t\tsummarizeBy: none

\t\tannotation SummarizationSetBy = Automatic

\t\tannotation UnderlyingDateTimeDataType = Date

\tpartition Date = calculated
\t\tmode: import
\t\tsource = CALENDAR ( DATE ( YEAR ( MIN ( Observations[period_date] ) ), 1, 1 ), DATE ( YEAR ( MAX ( Observations[period_date] ) ), 12, 31 ) )

"""


# ---------------------------------------------------------------------------
# Semantic model: measures
# ---------------------------------------------------------------------------

# Chart and card names come from the short_name column of series.csv

FORMAT_BY_UNITS = {"%": "0.0", "£m": "#,0", "thousands": "#,0", "number": "#,0", "index": "0.0"}


def measure_tmdl(name, expression, fmt="0.0", folder=None, description=None):
    lines = []
    if description:
        lines.append(f"\t/// {description}")
    expr_lines = expression.strip().splitlines()
    if len(expr_lines) == 1:
        lines.append(f"\tmeasure {q(name)} = {expr_lines[0]}")
    else:
        lines.append(f"\tmeasure {q(name)} =")
        lines += ["\t\t\t" + line for line in expr_lines]
    lines.append(f"\t\tformatString: {fmt}")
    if folder:
        lines.append(f"\t\tdisplayFolder: {folder}")
    lines.append(f"\t\tlineageTag: {tag('measure.' + name)}")
    return "\n".join(lines) + "\n"


def observation_measures(registry):
    out = [
        measure_tmdl("Series Value", "AVERAGE ( Observations[value] )",
                     description="Average of the values in context: the value itself for one series and period, the monthly average when daily data are shown by month."),
        # Variable names must not clash with DAX functions (LASTDATE is one).
        measure_tmdl("Latest Value", """
VAR LatestPeriod =
    CALCULATE ( MAX ( Observations[period_date] ), REMOVEFILTERS ( 'Date' ) )
RETURN
    CALCULATE ( [Series Value], Observations[period_date] = LatestPeriod, REMOVEFILTERS ( 'Date' ) )""",
                     fmt="0.00", description="Most recent value of the series in context, ignoring date filters."),
    ]
    # One measure per series, e.g. [CPI inflation (%)] = [Series Value] for cpi_yoy.
    # CALCULATE changes the filter context: here it restricts Series to one series_id.
    for row in registry:
        sid = row["series_id"]
        fmt = "0.00" if row["frequency"] == "D" and row["units"] == "%" else \
              "0.0000" if row["units"].endswith("per GBP") else FORMAT_BY_UNITS.get(row["units"], "0.0")
        out.append(measure_tmdl(row["short_name"], f'CALCULATE ( [Series Value], Series[series_id] = "{sid}" )',
                                fmt=fmt, folder=row["section"]))
        out.append(measure_tmdl(f"Latest: {row['short_name']}",
                                f'CALCULATE ( [Latest Value], Series[series_id] = "{sid}" )',
                                fmt=fmt, folder=f"Latest values\\{row['section']}"))
    out.append(measure_tmdl("Inflation target (%)", "IF ( NOT ISBLANK ( [CPI inflation (%)] ), 2 )",
                            folder="Prices", description="The Bank of England's 2% CPI target, shown only where CPI data exist."))
    return "".join(out)


def change_measures():
    def ch(sid, col):
        return f'CALCULATE ( AVERAGE ( Changes[{col}] ), Series[series_id] = "{sid}" )'
    return "".join([
        measure_tmdl("Monthly GDP growth, 3m/3m annualised (%)", ch("gdp_monthly", "change_3m3m_annualised"), folder="Activity"),
        measure_tmdl("Monthly GDP growth, y/y (%)", ch("gdp_monthly", "change_yoy"), folder="Activity"),
        measure_tmdl("Services output growth, 3m/3m annualised (%)", ch("services_output", "change_3m3m_annualised"), folder="Activity"),
        measure_tmdl("Retail sales volume growth, y/y (%)", ch("retail_sales_volume", "change_yoy"), folder="Households"),
        measure_tmdl("Mortgage approvals growth, y/y (%)", ch("mortgage_approvals", "change_yoy"), folder="Money and credit"),
    ])


def real_pay_measures():
    def rp(sid, col):
        return f'CALCULATE ( AVERAGE ( RealPay[{col}] ), RealPay[series_id] = "{sid}" )'
    return "".join([
        measure_tmdl("Real regular pay growth (%)", rp("regular_pay_yoy", "real_pay_growth"), folder="Labour market",
                     description="Whole-economy regular pay growth deflated by CPIH inflation (3-month averages)."),
        measure_tmdl("Real private regular pay growth (%)", rp("private_regular_pay_yoy", "real_pay_growth"), folder="Labour market"),
    ])


def yield_measures():
    return "".join([
        measure_tmdl("10y gilt minus Bank Rate (bp)", "AVERAGE ( YieldCurve[spread_10y_bank_rate_bp] )", fmt="0", folder="Rates"),
        measure_tmdl("10y minus 5y gilt (bp)", "AVERAGE ( YieldCurve[slope_10y_5y_bp] )", fmt="0", folder="Rates"),
        measure_tmdl("20y minus 5y gilt (bp)", "AVERAGE ( YieldCurve[slope_20y_5y_bp] )", fmt="0", folder="Rates"),
    ])


def real_rate_measures():
    return measure_tmdl("Real Bank Rate (%)", "AVERAGE ( RealPolicyRate[real_bank_rate] )", fmt="0.00", folder="Rates",
                        description="Monthly average Bank Rate minus CPI inflation (ex-post real policy rate).")


def psnb_measures():
    return "".join([
        measure_tmdl("Borrowing this fiscal year to date (£bn)", """
VAR LatestFY =
    CALCULATE ( MAX ( PsnbFiscalYear[fiscal_year] ), REMOVEFILTERS ( PsnbFiscalYear ), REMOVEFILTERS ( 'Date' ) )
RETURN
    DIVIDE ( CALCULATE ( SUM ( PsnbFiscalYear[psnb_fytd] ), PsnbFiscalYear[fiscal_year] = LatestFY, REMOVEFILTERS ( 'Date' ) ), 1000 )""",
                     folder="Public finances"),
        measure_tmdl("Borrowing last fiscal year to date (£bn)", """
VAR LatestFY =
    CALCULATE ( MAX ( PsnbFiscalYear[fiscal_year] ), REMOVEFILTERS ( PsnbFiscalYear ), REMOVEFILTERS ( 'Date' ) )
VAR PreviousFY =
    CALCULATE ( MAX ( PsnbFiscalYear[fiscal_year] ), PsnbFiscalYear[fiscal_year] < LatestFY, REMOVEFILTERS ( PsnbFiscalYear ), REMOVEFILTERS ( 'Date' ) )
RETURN
    DIVIDE ( CALCULATE ( SUM ( PsnbFiscalYear[psnb_fytd] ), PsnbFiscalYear[fiscal_year] = PreviousFY, REMOVEFILTERS ( 'Date' ) ), 1000 )""",
                     folder="Public finances"),
    ])


# ---------------------------------------------------------------------------
# Semantic model: relationships, parameters, model file
# ---------------------------------------------------------------------------

# (from table.column, to table.column): many-to-one, filters flow from the
# "one" side (Series, Date) to the fact tables. This is the star schema.
RELATIONSHIPS = [
    ("Observations", "series_id", "Series", "series_id"),
    ("Changes", "series_id", "Series", "series_id"),
    ("Observations", "period_date", "Date", "Date"),
    ("Changes", "period_date", "Date", "Date"),
    ("RealPay", "period_date", "Date", "Date"),
    ("YieldCurve", "period_date", "Date", "Date"),
    ("RealPolicyRate", "period_date", "Date", "Date"),
    ("PsnbFiscalYear", "period_date", "Date", "Date"),
]


def relationships_tmdl():
    out = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        lines = [f"relationship {tag(f'relationship.{ft}.{fc}.{tt}.{tc}')}"]
        if tc == "Date":
            lines.append("\tjoinOnDateBehavior: datePartOnly")
        lines += [f"\tfromColumn: {q(ft)}.{q(fc)}", f"\ttoColumn: {q(tt)}.{q(tc)}", ""]
        out.append("\n".join(lines))
    return "\n".join(out) + "\n"


def expressions_tmdl():
    folder = str(EXPORTS) + "\\"
    y, m, d = DASHBOARD_START
    return (f'expression ExportFolder = "{folder}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
            f"\tlineageTag: {tag('expression.ExportFolder')}\n\n"
            f"\tannotation PBI_ResultType = Text\n\n"
            f"expression DashboardStart = #date({y}, {m}, {d}) meta [IsParameterQuery=true, Type=\"Date\", IsParameterQueryRequired=true]\n"
            f"\tlineageTag: {tag('expression.DashboardStart')}\n\n"
            f"\tannotation PBI_ResultType = Date\n\n")


def model_tmdl(table_names):
    queries = ["ExportFolder", "DashboardStart"] + [t for t in table_names if t != "Date"]
    refs = "\n".join(f"ref table {q(t)}" for t in table_names)
    return f"""model Model
\tculture: en-US
\tdefaultPowerBIDataSourceVersion: powerBI_V3
\tsourceQueryCulture: en-NZ
\tvalueFilterBehavior: independent
\tdataAccessOptions
\t\tlegacyRedirects
\t\treturnErrorValuesAsNull

annotation __PBI_TimeIntelligenceEnabled = 0

annotation PBI_QueryOrder = {json.dumps(queries)}

annotation PBI_ProTooling = ["DevMode"]

{refs}

ref cultureInfo en-US

"""


def build_model():
    registry = list(csv.DictReader(open(ROOT / "series.csv", encoding="utf-8-sig")))
    tables_dir = MODEL_DIR / "tables"
    shutil.rmtree(tables_dir, ignore_errors=True)
    tables_dir.mkdir(parents=True)

    measures_by_table = {
        "Observations": observation_measures(registry),
        "Changes": change_measures(),
        "RealPay": real_pay_measures(),
        "YieldCurve": yield_measures(),
        "RealPolicyRate": real_rate_measures(),
        "PsnbFiscalYear": psnb_measures(),
    }
    names = []
    for spec in TABLES:
        text = table_tmdl(spec, measures_by_table.get(spec["name"], ""))
        (tables_dir / f"{spec['name']}.tmdl").write_text(text, encoding="utf-8")
        names.append(spec["name"])
    (tables_dir / "Date.tmdl").write_text(DATE_TABLE, encoding="utf-8")
    names.append("Date")

    (MODEL_DIR / "relationships.tmdl").write_text(relationships_tmdl(), encoding="utf-8")
    (MODEL_DIR / "expressions.tmdl").write_text(expressions_tmdl(), encoding="utf-8")
    (MODEL_DIR / "model.tmdl").write_text(model_tmdl(names), encoding="utf-8")
    # Let Desktop lay out the model diagram itself
    (MODEL_DIR.parent / "diagramLayout.json").write_text(json.dumps(
        {"version": "1.1.0", "diagrams": [{"ordinal": 0, "scrollPosition": {"x": 0, "y": 0}, "nodes": [],
                                           "name": "All tables", "zoomValue": 100}],
         "selectedDiagram": "All tables", "defaultDiagram": "All tables"}, indent=2), encoding="utf-8")
    return names


# ---------------------------------------------------------------------------
# Report pages
# ---------------------------------------------------------------------------

def lit(value):
    """A literal in a report formatting property. Strings are wrapped in single quotes."""
    return {"expr": {"Literal": {"Value": value}}}


def column(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def measure(entity, prop):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def projection(field):
    kind = "Column" if "Column" in field else "Measure"
    entity = field[kind]["Expression"]["SourceRef"]["Entity"]
    prop = field[kind]["Property"]
    return {"field": field, "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop}


def visual(visual_type, x, y, w, h, roles, title=None, objects=None):
    v = {"visualType": visual_type,
         "query": {"queryState": {role: {"projections": [projection(f) for f in fields]}
                                  for role, fields in roles.items()}},
         "drillFilterOtherVisuals": True}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = {
            "title": [{"properties": {"show": lit("true"),
                                      "text": lit("'" + title.replace("'", "''") + "'")}}],
            # Hide the automatic subtitle ("X and Y by Month Start"), which repeats the title
            "subTitle": [{"properties": {"show": lit("false")}}],
        }
    return {"$schema": VISUAL_SCHEMA, "name": "",  # set in build_report from page and position
            "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0},
            "visual": v}


OBS = "Observations"


def m(name, table=OBS):
    return measure(table, name)


def line(x, y, w, h, title, measures, axis=("Date", "Month Start")):
    return visual("lineChart", x, y, w, h, {"Category": [column(*axis)], "Y": measures}, title)


def columns_chart(x, y, w, h, title, measures, axis=("Date", "Month Start")):
    return visual("clusteredColumnChart", x, y, w, h, {"Category": [column(*axis)], "Y": measures}, title)


def card(x, y, w, h, name):
    # The card's title names the series, so hide the automatic label under the number
    return visual("card", x, y, w, h, {"Values": [m(f"Latest: {name}")]}, name,
                  objects={"categoryLabels": [{"properties": {"show": lit("false")}}]})


def table(x, y, w, h, title, entity, cols):
    return visual("tableEx", x, y, w, h, {"Values": [column(entity, c) for c in cols]}, title)


def date_slicer():
    return visual("slicer", 940, 10, 320, 70, {"Values": [column("Date", "Date")]},
                  objects={"data": [{"properties": {"mode": lit("'Between'")}}]})


W, H = 1280, 720
L, R = 20, 650          # left and right column x positions
CW = 610                 # chart width for two-column layouts
TOP = 90


def pages():
    return [
        ("Overview", [
            card(20, 20, 236, 110, "GDP growth q/q (%)"),
            card(268, 20, 236, 110, "CPI inflation (%)"),
            card(516, 20, 236, 110, "Unemployment rate (%)"),
            card(764, 20, 236, 110, "Bank Rate (%)"),
            card(1012, 20, 248, 110, "10-year gilt yield (%)"),
            table(20, 145, 1240, 560, "Latest data", "Snapshot",
                  ["section", "name", "latest_period", "latest_value", "change_period",
                   "change_yoy", "change_3m3m_annualised", "change_units"]),
        ]),
        ("Activity", [
            date_slicer(),
            columns_chart(L, TOP, CW, 300, "GDP growth, quarter on quarter (%)", [m("GDP growth q/q (%)")]),
            line(R, TOP, CW, 300, "Monthly GDP and services output (index)",
                 [m("Monthly GDP index"), m("Services output index")]),
            line(L, 405, CW, 300, "Monthly GDP growth (%)",
                 [m("Monthly GDP growth, 3m/3m annualised (%)", "Changes"), m("Monthly GDP growth, y/y (%)", "Changes")]),
            line(R, 405, CW, 300, "Retail sales volume growth, y/y (%)",
                 [m("Retail sales volume growth, y/y (%)", "Changes")]),
        ]),
        ("Labour market", [
            date_slicer(),
            line(L, TOP, CW, 300, "Unemployment rate, 16+ (%)", [m("Unemployment rate (%)")]),
            line(R, TOP, CW, 300, "Employment rate, 16-64 (%)", [m("Employment rate (%)")]),
            line(L, 405, CW, 300, "Vacancies (thousands)", [m("Vacancies (000s)")]),
            line(R, 405, CW, 300, "Regular pay growth, nominal and real (%)",
                 [m("Regular pay growth (%)"), m("Private regular pay growth (%)"),
                  m("Real regular pay growth (%)", "RealPay"), m("Real private regular pay growth (%)", "RealPay")]),
        ]),
        ("Inflation", [
            date_slicer(),
            line(L, TOP, 1240, 300, "CPI inflation against the 2% target (%)",
                 [m("CPI inflation (%)"), m("Core CPI inflation (%)"), m("CPIH inflation (%)"), m("Inflation target (%)")]),
            line(L, 405, 1240, 300, "Services and goods CPI inflation (%)",
                 [m("Services CPI inflation (%)"), m("Goods CPI inflation (%)")]),
        ]),
        ("Rates and money", [
            date_slicer(),
            line(L, TOP, CW, 300, "Policy rate and gilt yields, daily (%)",
                 [m("Bank Rate (%)"), m("SONIA (%)"), m("5-year gilt yield (%)"),
                  m("10-year gilt yield (%)"), m("20-year gilt yield (%)")], axis=("Date", "Date")),
            line(R, TOP, CW, 300, "Yield curve spreads, daily (bp)",
                 [m("10y gilt minus Bank Rate (bp)", "YieldCurve"), m("10y minus 5y gilt (bp)", "YieldCurve"),
                  m("20y minus 5y gilt (bp)", "YieldCurve")], axis=("Date", "Date")),
            line(L, 405, 400, 300, "Real Bank Rate (%)", [m("Real Bank Rate (%)", "RealPolicyRate")]),
            line(430, 405, 400, 300, "Mortgage rate and Bank Rate (%)",
                 [m("2-year fixed mortgage rate (%)"), m("Bank Rate (%)")]),
            columns_chart(840, 405, 420, 300, "Mortgage approvals", [m("Mortgage approvals")]),
        ]),
        ("Public finances and external", [
            date_slicer(),
            line(L, TOP, CW, 300, "Borrowing, fiscal year to date (£bn)",
                 [m("Borrowing this fiscal year to date (£bn)", "PsnbFiscalYear"),
                  m("Borrowing last fiscal year to date (£bn)", "PsnbFiscalYear")],
                 axis=("PsnbFiscalYear", "fy_month")),
            columns_chart(R, TOP, CW, 300, "Trade balance: total and goods only (£m)",
                          [m("Trade balance (£m)"), m("Goods trade balance (£m)")]),
            line(L, 405, CW, 300, "Sterling effective exchange rate (Jan 2005 = 100)", [m("Sterling ERI")]),
            line(R, 405, CW, 300, "Sterling against the dollar and euro", [m("GBP/USD"), m("GBP/EUR")]),
        ]),
        ("Data health", [
            table(20, 20, 1240, 680, "Data health by series", "DataHealth",
                  ["section", "name", "latest_period", "latest_vintage", "next_release",
                   "last_status", "health", "error_message"]),
        ]),
    ]


def build_report():
    pages_dir = REPORT_DIR / "pages"
    shutil.rmtree(pages_dir, ignore_errors=True)
    pages_dir.mkdir(parents=True)
    order = []
    for display_name, visuals in pages():
        pid = short_id("page." + display_name)
        order.append(pid)
        page_dir = pages_dir / pid
        (page_dir / "visuals").mkdir(parents=True)
        (page_dir / "page.json").write_text(json.dumps(
            {"$schema": PAGE_SCHEMA, "name": pid, "displayName": display_name,
             "displayOption": "FitToPage", "height": H, "width": W}, indent=2), encoding="utf-8")
        for i, v in enumerate(visuals):
            v["position"]["tabOrder"] = i
            v["position"]["z"] = i
            v["name"] = short_id(f"visual.{display_name}.{i}")
            vdir = page_dir / "visuals" / v["name"]
            vdir.mkdir()
            (vdir / "visual.json").write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")
    (pages_dir / "pages.json").write_text(json.dumps(
        {"$schema": PAGES_SCHEMA, "pageOrder": order, "activePageName": order[0]}, indent=2), encoding="utf-8")
    return len(order)


if __name__ == "__main__":
    tables = build_model()
    n_pages = build_report()
    print(f"Model: {len(tables)} tables ({', '.join(tables)})")
    print(f"Report: {n_pages} pages")
