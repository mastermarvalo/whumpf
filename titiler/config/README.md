# TiTiler config

Phase 2: when COGs are in MinIO, custom colormaps (e.g., the slope-angle
shading gradient: green < 30°, yellow 30-35°, orange 35-40°, red 40-45°,
purple > 45°) go here and get mounted into the container.

TiTiler uses environment variables and optionally `TITILER_` settings for
most behavior; this directory is for per-deployment overrides.

Ref: https://developmentseed.org/titiler/
