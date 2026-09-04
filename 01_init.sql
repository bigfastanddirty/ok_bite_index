-- 1. Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 2. Lakes reference table
CREATE TABLE IF NOT EXISTS lakes (
    lake_code VARCHAR(10) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    usace_office VARCHAR(5) DEFAULT 'SWT',
    latitude NUMERIC(8, 5) NOT NULL,
    longitude NUMERIC(8, 5) NOT NULL,
    normal_pool_ft NUMERIC(6, 2) NOT NULL,
    flood_pool_ft NUMERIC(6, 2)
);

-- Seed Arcadia Lake
INSERT INTO lakes (lake_code, name, usace_office, latitude, longitude, normal_pool_ft, flood_pool_ft)
VALUES ('ARCA', 'Arcadia Lake', 'SWT', 35.6483, -97.3631, 1006.00, 1029.50)
ON CONFLICT (lake_code) DO NOTHING;

-- 3. Lake Condition Time-Series Log
CREATE TABLE IF NOT EXISTS lake_readings (
    timestamp TIMESTAMPTZ NOT NULL,
    lake_code VARCHAR(10) NOT NULL REFERENCES lakes(lake_code),
    elevation_ft NUMERIC(6, 2),
    diff_from_normal_ft NUMERIC(5, 2),
    release_cfs NUMERIC(7, 1),
    inflow_cfs NUMERIC(7, 1),
    water_temp_f NUMERIC(4, 1),
    air_temp_f NUMERIC(4, 1),
    wind_speed_mph NUMERIC(4, 1),
    wind_gust_mph NUMERIC(4, 1),
    wind_direction_deg INT,
    surface_pressure_hpa NUMERIC(6, 1)
);

-- 4. Convert to Timescale Hypertable
SELECT create_hypertable('lake_readings', by_range('timestamp'), if_not_exists => TRUE);

-- Compound index to prevent duplicate records
CREATE UNIQUE INDEX IF NOT EXISTS idx_lake_time ON lake_readings (lake_code, timestamp DESC);
