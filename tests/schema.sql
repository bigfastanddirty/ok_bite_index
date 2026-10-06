CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE TABLE lake_readings (timestamp timestamp with time zone, lake_code character varying(10), elevation_ft numeric(6,2), diff_from_normal_ft numeric(5,2), release_cfs numeric(7,1), inflow_cfs numeric(7,1), water_temp_f numeric(4,1), air_temp_f numeric(4,1), wind_speed_mph numeric(4,1), wind_gust_mph numeric(4,1), wind_direction_deg integer, surface_pressure_hpa numeric(6,1), dissolved_oxygen_mg_l numeric(5,2), conductance_us_cm numeric(7,1), ph numeric(4,2), tailwater_elevation_ft numeric(6,2), cloud_cover_pct numeric(4,1), precipitation_in numeric(5,2), uv_index numeric(4,1));
CREATE UNIQUE INDEX idx_lake_time ON lake_readings(lake_code,timestamp);
CREATE TABLE lakes (lake_code character varying(10), name character varying(100), usace_office character varying(5), latitude numeric(8,5), longitude numeric(8,5), normal_pool_ft numeric(6,2), flood_pool_ft numeric(6,2), usgs_site_id character varying(20), special_regulations text, target_species text[]);
INSERT INTO lakes(lake_code,name,latitude,longitude,normal_pool_ft,target_species) VALUES
('HEFN','Lake Hefner',35.5852,-97.5992,1199,ARRAY['Largemouth Bass','Crappie']),
('FOSS','Foss Lake',35.54,-99.19,1652,ARRAY['Largemouth Bass','Walleye']);
