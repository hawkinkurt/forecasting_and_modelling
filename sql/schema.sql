CREATE TABLE IF NOT EXISTS series (
    series_id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    dataset VARCHAR,
    name VARCHAR NOT NULL,
    frequency VARCHAR NOT NULL,
    area VARCHAR NOT NULL,
    units VARCHAR NOT NULL,
    next_release DATE,
    last_fetched TIMESTAMP
);

-- Columns added after the table was first created. IF NOT EXISTS makes these
-- safe to re-run, so this file works on both new and existing databases.
ALTER TABLE series ADD COLUMN IF NOT EXISTS section VARCHAR;
ALTER TABLE series ADD COLUMN IF NOT EXISTS measure VARCHAR;  -- 'rate' or 'level'

CREATE TABLE IF NOT EXISTS observations (
    series_id VARCHAR NOT NULL,
    period_date DATE NOT NULL,
    value DOUBLE NOT NULL,
    vintage_date DATE NOT NULL,
    fetched_at TIMESTAMP,
    PRIMARY KEY (series_id, period_date, vintage_date)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    fetched_at TIMESTAMP NOT NULL,
    series_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    rows_received INTEGER,
    rows_written INTEGER,
    message VARCHAR
);

CREATE TABLE IF NOT EXISTS forecasts (
    series_id VARCHAR NOT NULL,
    period_date DATE NOT NULL,
    forecast_value DOUBLE NOT NULL,
    date_made DATE NOT NULL,
    model VARCHAR NOT NULL,
    message VARCHAR,
    PRIMARY KEY (series_id, period_date, date_made, model)
);
