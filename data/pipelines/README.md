# DEM pipelines

Phase 2 scripts that download from USGS 3DEP, mosaic/reproject, compute
derivatives (slope/aspect/hillshade), convert to COG, and push to MinIO.

Target entrypoint: `python dem_pipeline.py --bbox <w,s,e,n> --resolution 10`

# Ski run tiles

`ski_tiles.py` + `ski/` (tilemaker profile, config, region list) build
`ski_<region>.mbtiles` from a Geofabrik OSM extract. Run through make:
`make ski-tiles-refresh REGION=colorado`. Details, layers and fields:
docs/runbook.md §7 "Ski run tiles".
