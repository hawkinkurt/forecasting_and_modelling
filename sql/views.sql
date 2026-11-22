-- Views: everything derived from the stored data lives here, never in the tables.
-- Safe to re-run: CREATE OR REPLACE rebuilds each view from the current tables.
-- Views later in the file can build on views earlier in the file.


-- ===========================================================================
-- Core views
-- ===========================================================================

-- Latest estimate of every period for every series.
-- observations keeps every vintage, so for each (series_id, period_date) we keep
-- only the row with the most recent vintage_date. QUALIFY filters on a window
-- function result, which saves wrapping the query in a subquery.
CREATE OR REPLACE VIEW latest_observations AS
SELECT
    series_id,
    period_date,
    value,
    vintage_date
FROM observations
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY series_id, period_date
    ORDER BY vintage_date DESC
) = 1;

-- Latest estimates with series metadata attached, in the long ("tidy") shape
-- Power BI prefers: one row per series per period. LEFT JOIN so data still shows
-- up for series that have not been registered in the series table yet.
CREATE OR REPLACE VIEW latest_observations_labelled AS
SELECT
    o.series_id,
    COALESCE(s.name, o.series_id) AS series_name,
    s.section,
    s.measure,
    s.source,
    s.area,
    s.frequency,
    s.units,
    o.period_date,
    o.value,
    o.vintage_date
FROM latest_observations AS o
LEFT JOIN series AS s USING (series_id);


-- ===========================================================================
-- Common frequency
-- ===========================================================================

-- Monthly and quarterly series as published, plus daily series averaged to
-- monthly, so every series can be compared on the same calendar.
-- Monthly averages (not month-end values) are the convention for rates and
-- exchange rates in macro work: they smooth out day-to-day noise.
-- is_complete is false for the current month of a daily series, whose average
-- will still change as more days come in.
CREATE OR REPLACE VIEW analysis_series AS
SELECT
    o.series_id,
    s.frequency,
    s.measure,
    o.period_date,
    o.value,
    TRUE AS is_complete
FROM latest_observations AS o
JOIN series AS s USING (series_id)
WHERE s.frequency IN ('M', 'Q')

UNION ALL

SELECT
    o.series_id,
    'M' AS frequency,
    s.measure,
    date_trunc('month', o.period_date)::DATE AS period_date,
    AVG(o.value) AS value,
    date_trunc('month', o.period_date) < date_trunc('month', current_date) AS is_complete
FROM latest_observations AS o
JOIN series AS s USING (series_id)
WHERE s.frequency = 'D'
GROUP BY ALL;  -- DuckDB shorthand: group by every column that is not aggregated


-- ===========================================================================
-- Changes and growth rates
-- ===========================================================================

-- For every monthly and quarterly series (daily ones via their monthly
-- averages), the change on the previous period, on a year earlier and, for
-- monthly levels, 3-month-on-3-month annualised growth.
--
-- How a change is measured depends on what the series is (series.measure):
--   level   (indexes, £m stocks): % change
--   rate    (inflation, unemployment, yields): percentage-point change
--   balance (borrowing, trade balance): change in its own units (£m)
--
-- The windows use RANGE with calendar intervals rather than ROWS. "RANGE
-- BETWEEN INTERVAL 12 MONTH PRECEDING AND INTERVAL 12 MONTH PRECEDING" means
-- "the row dated exactly 12 months earlier", so a missing month gives NULL
-- instead of silently comparing the wrong months (which "12 rows back" would).
CREATE OR REPLACE VIEW changes AS
WITH windows AS (
    SELECT
        *,
        FIRST_VALUE(value) OVER (w RANGE BETWEEN INTERVAL 1 MONTH PRECEDING AND INTERVAL 1 MONTH PRECEDING) AS value_1m_ago,
        FIRST_VALUE(value) OVER (w RANGE BETWEEN INTERVAL 3 MONTH PRECEDING AND INTERVAL 3 MONTH PRECEDING) AS value_3m_ago,
        FIRST_VALUE(value) OVER (w RANGE BETWEEN INTERVAL 12 MONTH PRECEDING AND INTERVAL 12 MONTH PRECEDING) AS value_12m_ago,
        -- Averages of the latest 3 months and the 3 months before, with counts so
        -- we only use them when all 3 months are present.
        AVG(value)   OVER (w RANGE BETWEEN INTERVAL 2 MONTH PRECEDING AND CURRENT ROW) AS avg_latest_3m,
        COUNT(value) OVER (w RANGE BETWEEN INTERVAL 2 MONTH PRECEDING AND CURRENT ROW) AS n_latest_3m,
        AVG(value)   OVER (w RANGE BETWEEN INTERVAL 5 MONTH PRECEDING AND INTERVAL 3 MONTH PRECEDING) AS avg_prior_3m,
        COUNT(value) OVER (w RANGE BETWEEN INTERVAL 5 MONTH PRECEDING AND INTERVAL 3 MONTH PRECEDING) AS n_prior_3m
    FROM analysis_series
    WINDOW w AS (PARTITION BY series_id ORDER BY period_date)
),
bases AS (
    SELECT
        *,
        -- "Previous period" is a month for monthly data, a quarter for quarterly
        CASE frequency WHEN 'Q' THEN value_3m_ago ELSE value_1m_ago END AS value_prev_period
    FROM windows
)
SELECT
    series_id,
    frequency,
    measure,
    period_date,
    value,
    is_complete,
    CASE measure
        WHEN 'level' THEN (value / value_prev_period - 1) * 100
        ELSE value - value_prev_period
    END AS change_period,
    CASE measure
        WHEN 'level' THEN (value / value_12m_ago - 1) * 100
        ELSE value - value_12m_ago
    END AS change_yoy,
    -- (average of latest 3 months / average of previous 3 months)^4 - 1:
    -- growth over the latest quarter, scaled up to an annual rate. Responds to
    -- turning points faster than y/y but is less noisy than m/m.
    CASE
        WHEN measure = 'level' AND frequency = 'M' AND n_latest_3m = 3 AND n_prior_3m = 3
        THEN (POWER(avg_latest_3m / avg_prior_3m, 4) - 1) * 100
    END AS change_3m3m_annualised,
    CASE measure WHEN 'level' THEN '%' WHEN 'rate' THEN 'pp' ELSE 'units' END AS change_units
FROM bases;


-- ===========================================================================
-- Labour market: real pay
-- ===========================================================================

-- Nominal pay growth deflated by CPIH inflation, following the ONS convention
-- of using CPIH for real earnings. Pay growth is a 3-month average of y/y
-- growth, so inflation is averaged over the same 3 months to match.
-- Real growth = (1 + nominal) / (1 + inflation) - 1, the exact form rather
-- than the "nominal minus inflation" shortcut. This is close to, but not
-- identical to, the ONS's own real AWE series (which deflates pay levels).
CREATE OR REPLACE VIEW real_pay AS
WITH cpih_3m AS (
    SELECT
        period_date,
        AVG(value)   OVER (ORDER BY period_date RANGE BETWEEN INTERVAL 2 MONTH PRECEDING AND CURRENT ROW) AS cpih_3m_avg,
        COUNT(value) OVER (ORDER BY period_date RANGE BETWEEN INTERVAL 2 MONTH PRECEDING AND CURRENT ROW) AS n_months
    FROM latest_observations
    WHERE series_id = 'cpih_yoy'
)
SELECT
    p.series_id,
    p.period_date,
    p.value AS nominal_pay_growth,
    c.cpih_3m_avg AS cpih_inflation_3m_avg,
    ((1 + p.value / 100) / (1 + c.cpih_3m_avg / 100) - 1) * 100 AS real_pay_growth
FROM latest_observations AS p
JOIN cpih_3m AS c USING (period_date)
WHERE p.series_id IN ('regular_pay_yoy', 'private_regular_pay_yoy')
  AND c.n_months = 3;


-- ===========================================================================
-- Rates: yield curve and real policy rate
-- ===========================================================================

-- Daily yield curve measures, in basis points (1bp = 0.01 percentage points).
-- The pivot uses conditional aggregation: MAX(...) FILTER (WHERE ...) picks
-- one series' value per date, turning long data into one column per series.
--   10y minus Bank Rate: how far long-term borrowing costs sit above the
--     policy rate. Negative (an inverted curve) usually means markets expect
--     rate cuts, often because they expect a slowdown.
--   10y minus 5y and 20y minus 5y: the slope of the curve beyond the short end,
--     which reflects expected future rates plus the term premium (the extra
--     return investors demand for holding longer-dated bonds).
CREATE OR REPLACE VIEW yield_curve AS
WITH daily AS (
    SELECT
        period_date,
        MAX(value) FILTER (WHERE series_id = 'bank_rate') AS bank_rate,
        MAX(value) FILTER (WHERE series_id = 'sonia')     AS sonia,
        MAX(value) FILTER (WHERE series_id = 'gilt_5y')   AS gilt_5y,
        MAX(value) FILTER (WHERE series_id = 'gilt_10y')  AS gilt_10y,
        MAX(value) FILTER (WHERE series_id = 'gilt_20y')  AS gilt_20y
    FROM latest_observations
    WHERE series_id IN ('bank_rate', 'sonia', 'gilt_5y', 'gilt_10y', 'gilt_20y')
    GROUP BY period_date
)
SELECT
    *,
    (gilt_10y - bank_rate) * 100 AS spread_10y_bank_rate_bp,
    (gilt_10y - gilt_5y) * 100   AS slope_10y_5y_bp,
    (gilt_20y - gilt_5y) * 100   AS slope_20y_5y_bp
FROM daily
WHERE gilt_10y IS NOT NULL;

-- Monthly average Bank Rate minus CPI inflation: the ex-post real policy rate.
-- Positive means policy is restrictive in real terms. "Ex post" because it
-- uses actual inflation; the ex-ante version would use expected inflation.
CREATE OR REPLACE VIEW real_policy_rate AS
SELECT
    b.period_date,
    b.value AS bank_rate_avg,
    c.value AS cpi_inflation,
    b.value - c.value AS real_bank_rate
FROM analysis_series AS b
JOIN latest_observations AS c
    ON c.series_id = 'cpi_yoy' AND c.period_date = b.period_date
WHERE b.series_id = 'bank_rate';


-- ===========================================================================
-- Public finances: borrowing by fiscal year
-- ===========================================================================

-- PSNB cumulated over the fiscal year (April to March), compared with the
-- same point of the previous fiscal year. Year-to-date borrowing against last
-- year (and against the OBR forecast) is how the monthly release is read.
CREATE OR REPLACE VIEW psnb_fiscal_year AS
WITH tagged AS (
    SELECT
        period_date,
        value AS psnb_month,
        -- April starts a new fiscal year: Apr 2026 to Mar 2027 is "2026-27"
        CASE WHEN month(period_date) >= 4 THEN year(period_date) ELSE year(period_date) - 1 END AS fy_start,
        (month(period_date) + 8) % 12 + 1 AS month_of_fy  -- April = 1 ... March = 12
    FROM latest_observations
    WHERE series_id = 'psnb'
),
cumulative AS (
    SELECT
        *,
        SUM(psnb_month) OVER (PARTITION BY fy_start ORDER BY period_date) AS psnb_fytd
    FROM tagged
)
SELECT
    c.period_date,
    c.fy_start || '-' || right(CAST(c.fy_start + 1 AS VARCHAR), 2) AS fiscal_year,
    c.month_of_fy,
    c.psnb_month,
    c.psnb_fytd,
    p.psnb_fytd AS psnb_fytd_last_year,
    c.psnb_fytd - p.psnb_fytd AS fytd_change_on_last_year
FROM cumulative AS c
LEFT JOIN cumulative AS p
    ON p.fy_start = c.fy_start - 1 AND p.month_of_fy = c.month_of_fy;


-- ===========================================================================
-- Revisions
-- ===========================================================================

-- First and latest estimate of every period. arg_min(value, vintage_date)
-- returns the value from the row with the earliest vintage.
-- Caveat: the first time a series is loaded, its whole history arrives in one
-- vintage, so those "first estimates" are really whatever was current on that
-- day. is_backfill flags them. True first estimates build up from then on.
CREATE OR REPLACE VIEW revisions AS
WITH per_period AS (
    SELECT
        series_id,
        period_date,
        arg_min(value, vintage_date) AS first_estimate,
        MIN(vintage_date) AS first_vintage,
        arg_max(value, vintage_date) AS latest_estimate,
        MAX(vintage_date) AS latest_vintage,
        COUNT(*) AS n_vintages
    FROM observations
    GROUP BY series_id, period_date
)
SELECT
    *,
    latest_estimate - first_estimate AS total_revision,
    first_vintage = MIN(first_vintage) OVER (PARTITION BY series_id) AS is_backfill
FROM per_period;


-- ===========================================================================
-- Dashboard summaries
-- ===========================================================================

-- One row per series: the latest value and its latest changes. For daily
-- series the latest value is the latest day, while the changes are on monthly
-- averages of the latest complete month (a part-month average would mislead).
CREATE OR REPLACE VIEW latest_snapshot AS
WITH latest_value AS (
    SELECT series_id, period_date, value
    FROM latest_observations
    QUALIFY ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY period_date DESC) = 1
),
latest_change AS (
    SELECT *
    FROM changes
    WHERE is_complete
    QUALIFY ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY period_date DESC) = 1
)
SELECT
    s.section,
    s.series_id,
    s.name,
    s.frequency,
    s.units,
    v.period_date AS latest_period,
    v.value AS latest_value,
    c.period_date AS change_period_date,
    c.change_period,
    c.change_yoy,
    c.change_3m3m_annualised,
    c.change_units,
    s.next_release
FROM series AS s
JOIN latest_value AS v USING (series_id)
LEFT JOIN latest_change AS c USING (series_id);

-- Is every series healthy? Combines the fetch log with how recent the data are.
--   error:   the last fetch failed
--   overdue: a scheduled release date has passed but no new data arrived
--            (or the pipeline has not been run since)
--   stale:   the latest data point is older than expected for its frequency
CREATE OR REPLACE VIEW data_health AS
WITH last_fetch AS (
    SELECT series_id, status, message, fetched_at
    FROM fetch_log
    QUALIFY ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY fetched_at DESC) = 1
),
last_success AS (
    SELECT series_id, MAX(fetched_at) AS last_success
    FROM fetch_log
    WHERE status IN ('ok', 'no change')
    GROUP BY series_id
),
last_data AS (
    SELECT series_id, MAX(period_date) AS latest_period, MAX(vintage_date) AS latest_vintage
    FROM observations
    GROUP BY series_id
)
SELECT
    s.section,
    s.series_id,
    s.name,
    s.frequency,
    d.latest_period,
    d.latest_vintage,
    s.next_release,
    f.fetched_at AS last_fetch,
    f.status AS last_status,
    k.last_success,
    CASE
        WHEN f.status = 'error' THEN 'error'
        WHEN s.next_release < current_date THEN 'overdue'
        WHEN s.frequency = 'D' AND d.latest_period < current_date - INTERVAL 10 DAY THEN 'stale'
        WHEN s.frequency = 'M' AND d.latest_period < current_date - INTERVAL 5 MONTH THEN 'stale'
        WHEN s.frequency = 'Q' AND d.latest_period < current_date - INTERVAL 8 MONTH THEN 'stale'
        WHEN d.latest_period IS NULL THEN 'no data'
        ELSE 'ok'
    END AS health,
    CASE WHEN f.status = 'error' THEN f.message END AS error_message
FROM series AS s
LEFT JOIN last_data AS d USING (series_id)
LEFT JOIN last_fetch AS f USING (series_id)
LEFT JOIN last_success AS k USING (series_id);
