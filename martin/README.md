# Martin config

Martin (127.0.0.1:3000, loopback only) serves:

- PostGIS tables, discovered automatically at startup.
- Every `.mbtiles` in `/data/martin-tiles` (mounted read-only), source id =
  file name: `terrain_rgb`, `terrain_rgb_hires`, `ski_<region>`.

It only scans at startup, so new or replaced files need `make restart-martin`.
`make ski-tiles-publish` does that itself (docs/runbook.md §7). Never leave
stray `.mbtiles` in `/data/martin-tiles`: each one becomes a source.

Clients reach ski tiles through the API (`/tiles/ski/...`), not Martin.

No `config.yml` yet. Add one (read-only mount in compose.yml) only to override
function signatures, zoom ranges or non-default schemas.

Ref: https://maplibre.org/martin/sources-pg-tables.html
