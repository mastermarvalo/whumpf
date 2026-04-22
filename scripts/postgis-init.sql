-- -----------------------------------------------------------------------------
-- Whumpf PostGIS initialization
--
-- This file runs automatically on the *first* boot of the postgis container
-- (it is placed in /docker-entrypoint-initdb.d by the compose file). It is
-- executed against the database specified by POSTGRES_DB.
--
-- On subsequent boots, the initdb directory is ignored — so schema changes
-- after this point should be handled by Alembic migrations in the backend,
-- not by editing this file.
-- -----------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- For fuzzy place-name lookups later (e.g., "Silverton", "Red Mountain Pass").
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- btree_gist lets us build composite indexes that combine plain columns with
-- spatial / range columns — useful for e.g. (zone_id, valid_range) indexes on
-- daily avalanche forecasts.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Raster out-of-database access must be enabled explicitly in PostGIS.
-- This lets us reference raster data stored outside the DB (in MinIO) from
-- within PostGIS functions if we ever want to. Default is NONE.
ALTER DATABASE :"POSTGRES_DB" SET postgis.enable_outdb_rasters = true;
ALTER DATABASE :"POSTGRES_DB" SET postgis.gdal_enabled_drivers = 'ENABLE_ALL';

-- Sanity check: log versions so they show up in the container log.
DO $$
BEGIN
    RAISE NOTICE 'PostGIS version: %', PostGIS_Full_Version();
END $$;
