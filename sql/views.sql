-- Views used for analysis and for the Power BI exports.
-- Safe to re-run: CREATE OR REPLACE rebuilds them from the current tables.

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
    s.source,
    s.area,
    s.frequency,
    s.units,
    o.period_date,
    o.value,
    o.vintage_date
FROM latest_observations AS o
LEFT JOIN series AS s USING (series_id);
