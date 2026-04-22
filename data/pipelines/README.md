# DEM pipelines

Phase 2 scripts that download from USGS 3DEP, mosaic/reproject, compute
derivatives (slope/aspect/hillshade), convert to COG, and push to MinIO.

Target entrypoint: `python dem_pipeline.py --bbox <w,s,e,n> --resolution 10`
