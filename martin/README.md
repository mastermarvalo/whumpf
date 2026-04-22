# Martin config

Phase 3: when CAIC zones and user tables start getting ingested, drop a
`config.yml` here and wire it into compose.yml via a read-only mount.
Martin discovers PostGIS tables automatically, so config only becomes
necessary when you want to override function signatures, set specific
zoom ranges, or expose non-default schemas.

Ref: https://maplibre.org/martin/sources-pg-tables.html
