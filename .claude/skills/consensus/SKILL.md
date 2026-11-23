---
name: consensus
description: Read market consensus figures from the Bloomberg economic calendar, pasted as text (Ctrl+A copy of the page) or as screenshots (in consensus_inbox/ or pasted into the chat), and add them to consensus_inbox/consensus.csv for the monitoring pipeline. Use when the user types /consensus, pastes calendar text, or asks to read consensus or forecasts from the calendar.
---

# Read consensus figures from the economic calendar

The monitoring pipeline loads `consensus_inbox/consensus.csv` on every run and shows the figures
in the email (consensus against upcoming releases, surprise once the data is out). Your job is to
turn screenshots into correct rows in that file. Accuracy matters more than coverage: a wrong
figure produces a wrong "surprise", so leave out anything you cannot read with confidence.

## 1. Find the input

Any of:
- **Pasted page text** (the usual route: the user selects all on the calendar page and pastes it).
  Ignore the time-zone list, filters and bullet noise at the top. Each event looks like:

  ```
  Friday, December 11, 2026
  * 02:00
    GBP
  GDP (MoM) (Oct)
  Act:
  Cons: 0.1%
  Prev:0.2%
  ```

  The day header gives the release date; `Cons:` is the consensus; `Act:` and `Prev:` are not.
  Only `GBP` events are UK. The times are in the page's chosen time zone (often US Eastern, e.g.
  UK 07:00 shows as 02:00): ignore them, the pipeline takes exact UK times from the ONS calendar.
- Images pasted into this conversation.
- Image files (`.png`, `.jpg`, `.jpeg`, `.webp`) directly in `consensus_inbox/` (not in `processed/`).

If there is no input, say so and stop. If nothing in it has a consensus for a tracked series,
add no rows and say which events were skipped and why.

## 2. Know the series

Read `series.csv`. Only UK series (`area` = UK) can take consensus figures. Note each series'
`frequency`, `units` and `measure`. To see the latest period already published for a series, run:

```powershell
.\.venv\Scripts\python.exe -c "from pipeline.db import connect; c=connect(read_only=True); print(c.execute('SELECT series_id, max(period_date) FROM observations GROUP BY 1 ORDER BY 1').fetchall())"
```

## 3. Read each screenshot

Open each image (Read tool for files) and extract only UK events that match a series:

- Use the **consensus / survey / median** column. Never the prior, actual or revised columns.
  An event with no consensus figure is skipped.
- `period` is the **reference period** of the data, not the release date: `YYYY-MM` monthly,
  `YYYY-Qn` quarterly, `YYYY-MM-DD` for daily series such as a Bank Rate decision. Calendars show it
  in brackets, e.g. "CPI YoY (Nov)". Pick the year from the release dates on the screenshot.
- `basis`:
  - `value` for the series itself: rates (CPI y/y, unemployment rate, pay growth, Bank Rate) and
    balances (borrowing, trade balance). Series with `measure` rate or balance always use `value`.
  - `mom`, `qoq` or `yoy` for a % change of a level series, e.g. "Retail Sales Inc Auto Fuel MoM"
    is `retail_sales_volume` with basis `mom`; "Monthly GDP (MoM)" is `gdp_monthly` with `mom`.
- Convert to the series' units: borrowing "17.5B" becomes 17500 for a series in GBP millions;
  "53.44K" mortgage approvals becomes 53440; percentages are plain numbers (3.5 for 3.5%).
- Typical mappings (check the screenshot's wording, do not assume):
  CPI YoY -> `cpi_yoy`; CPI Core YoY -> `core_cpi_yoy`; CPI Services YoY -> `services_cpi_yoy`;
  CPIH YoY -> `cpih_yoy`; ILO Unemployment Rate 3Mths -> `unemployment_rate`;
  Average Weekly Earnings ex Bonus 3M/YoY -> `regular_pay_yoy`; GDP QoQ -> `gdp_qoq` (value);
  GDP (MoM) / Monthly GDP (MoM) -> `gdp_monthly` (mom); GDP (YoY) (monthly, e.g. "(Oct)") -> `gdp_monthly` (yoy);
  Retail Sales (MoM) / (YoY) -> `retail_sales_volume` (mom / yoy) when it is the headline including fuel;
  Mortgage Approvals -> `mortgage_approvals` (value, number);
  Public Sector Net Borrowing (ex banks) -> `psnb`; Bank of England Bank Rate / Interest Rate Decision -> `bank_rate`.
- Do **not** map:
  - "Trade Balance" / "Trade Balance Non-EU": on this calendar these are the goods (visible) balance in
    GBP billions, not our total goods and services `trade_balance`. Only map an explicitly total
    goods-and-services balance.
  - "Monthly GDP 3M/3M Change": a 3-month-on-3-month rate that is not annualised, unlike our measure.
  - Index of Services 3M/3M, PMIs, CBI surveys, house price indices, construction and production: not
    series we hold.
- If a figure is ambiguous (unclear period, unit or which measure), skip it and say why.

## 4. Write the rows

Append to `consensus_inbox/consensus.csv`, creating it with this header if it does not exist:

```
series_id,period,basis,consensus,as_of,event_as_shown,source
```

- `as_of`: today's date (YYYY-MM-DD), unless the user says the screenshot was taken on another day.
- `event_as_shown`: the event name exactly as on the calendar (quote it if it contains a comma).
- `source`: `Bloomberg`.
- Do not add a row that duplicates an existing one (same series_id, period, basis and as_of); if the
  figure changed, update that row instead.

Then move each processed image file to `consensus_inbox/processed/`, prefixed with the date
(`YYYY-MM-DD_<name>`). Pasted images have no file to move.

## 5. Check and report

Run the loader to validate the file against the registry:

```powershell
.\.venv\Scripts\python.exe -c "from pipeline.db import connect, ensure_schema; from pipeline.consensus import load_consensus; c=connect(); ensure_schema(c); rows, problems = load_consensus(c); print(len(rows), 'new rows'); print(*problems, sep=chr(10))"
```

Fix any problems it reports. Finish with a table for the user to check against the screenshot:
event as shown, series, period, basis, consensus, plus anything you skipped and why.
The next `run.py` puts the figures into the email.
