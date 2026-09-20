-- Enable PostGIS extension
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- Create spatial reference systems cache table
CREATE TABLE IF NOT EXISTS spatial_ref_sys (
    srid INTEGER NOT NULL PRIMARY KEY,
    auth_name VARCHAR(256),
    auth_srid INTEGER,
    srtext VARCHAR(2048),
    proj4text VARCHAR(2048)
);

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO fire_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO fire_user;
