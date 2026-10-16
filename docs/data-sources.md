# Whumpf data sources

Authoritative list of every external data source Whumpf consumes, with
endpoints, access patterns, licensing, and gotchas. Update this file
whenever a feed is added, removed, or changes its access pattern.

## Phase 1 feeds

### CAIC — Colorado Avalanche Information Center

**Use:** daily backcountry forecast zones with danger ratings and forecast
text.

- Homepage: <https://avalanche.state.co.us/>
- Zone map: <https://avalanche.state.co.us/forecasts/backcountry-avalanche>

**Access pattern:** the CAIC **does not publish a documented public API**.
The data is fetched by their own SPA at runtime. For a personal project,
acceptable approaches are:

1. Inspect network requests from the CAIC homepage in browser devtools and
   call the internal JSON endpoints directly. Be prepared for breakage when
   they redeploy.
2. Scrape the rendered HTML nightly for forecast text.
3. Ingest the zone polygons once (they rarely change) and only poll the
   small JSON that carries danger ratings.

The polygon geometry for the ten Colorado forecast zones is effectively
static season-to-season; it's the daily ratings that move. Zone IDs and
names should be pinned as part of the `caic_zones` table schema so a
boundary change doesn't silently orphan historical data.

**Licensing:** CAIC forecasts are public-interest information published by
a state agency. Redistribution is not explicitly licensed. For a personal,
self-hosted app this is fine; do not rebroadcast to third parties.

**Update frequency:** twice daily (roughly 04:30 and 16:30 local).

---

### SNOTEL — NRCS Snow Telemetry

**Use:** snowpack depth, snow water equivalent (SWE), precipitation, air
temperature at automated stations across the western U.S.

- Landing page: <https://www.nrcs.usda.gov/programs-initiatives/sswsf-snow-survey-and-water-supply-forecasting-program>
- Web service: AWDB REST API v1, `https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1`
- Station metadata & interactive map: <https://www.nrcs.usda.gov/resources/data-and-reports/air-and-water-database-report-generator>

**Access pattern:** plain JSON over HTTP, no key (`app/services/snotel.py`).
`/stations` (filter to `stateCode == CO`, `networkCode == SNTL` client-side;
the API ignores those params) and `/data` with `duration=DAILY`,
`centralTendencyType=MEDIAN`, in batches of 40 station triplets. The station
list is cached for 6 h.

Element codes: `WTEQ` (snow water equivalent), `SNWD` (snow depth), `PREC`
(precipitation accumulation), `TOBS` (observed air temperature). Whumpf
fetches `WTEQ,SNWD,TOBS`. Daily `WTEQ`/`SNWD` are start-of-day (midnight)
readings; the 24 h / 72 h new snow at each station is derived from them
(docs/runbook.md §8).

**Licensing:** U.S. federal government data, public domain.

**Update frequency:** 1–3 hours per station.

---

### NOAA NDFD — National Digital Forecast Database

**Use:** gridded forecast (temperature, wind, precipitation, snow amount)
at the 2.5 km CONUS grid.

- API documentation: <https://www.weather.gov/documentation/services-web-api>
- Points endpoint: `https://api.weather.gov/points/{lat},{lon}`
- Forecast: follow the `forecast` / `forecastGridData` link from the points
  response.

**Access pattern:** modern JSON-LD REST API. No key needed. Rate limits
are generous but not documented; polling every few minutes per point is
fine.

**Gotcha:** the `points` endpoint returns grid coordinates (`gridX`,
`gridY`, and the forecasting office). These can change — the docs say to
re-resolve them periodically rather than caching indefinitely.

**Licensing:** U.S. federal government data, public domain.

**Update frequency:** `forecastGridData` updates roughly hourly; textual
forecast ~6 hours.

---

### NOAA NOHRSC — National Snowfall Analysis

**Use:** the Recent snowfall layer, 24 h and 72 h new snow (docs/runbook.md §8).

- Product page: <https://www.nohrsc.noaa.gov/snowfall/>
- Files: `https://www.nohrsc.noaa.gov/snowfall/data/YYYYMM/sfav2_CONUS_{6h,24h,48h,72h}_YYYYMMDDHH.{tif,nc,png}`,
  plus season-to-date `sfav2_CONUS_<YYYYMMDD>12_to_<YYYYMMDD>12.*`
- Product description: `snowfall/data/resources/documents/PDD_National Gridded Snowfall Analysis.pdf`

**Access pattern:** hourly conditional GET of the GeoTIFFs (~0.25–0.5 MB
each). There is no snowfall layer on `mapservices.weather.noaa.gov`
(`snow/NOHRSC_Snow_Analysis` has snow depth and SWE only) and no WMS.

**Gotchas:** files are overwritten in place for five days as late reports
arrive (track `ETag`); TIFs are in inches but NetCDFs in metres; the 48 h /
72 h TIFs lack the NoData tag (-99999); grid is EPSG:4326 at 0.04°, not a
COG. `robots.txt` disallows `*.nc` and `*.png`.

**Licensing:** NWS data, public domain (<https://www.weather.gov/disclaimer>):
credit NOAA/NWS NOHRSC, don't imply endorsement.

**Update frequency:** 24 h/48 h/72 h at 00Z and 12Z, 6 h every 6 hours;
first file about an hour after the valid time, then reruns hourly.

---

### NOAA NOHRSC — Snow depth (map service)

The old site's "Snow depth" overlay calls
`https://mapservices.weather.noaa.gov/raster/rest/services/snow/NOHRSC_Snow_Analysis/MapServer/export`
(`layers=show:0`, `bboxSR=imageSR=3857`) straight from the browser
(`frontend/src/components/Map/layers/overlays.ts`). ~1 km, not time-enabled,
updated at 01, 05, 11 and 17Z. Its WMS has no EPSG:3857, so use the REST
export for web tiles.

---

### USGS 3DEP — 3D Elevation Program

**Use:** bare-earth DEMs for deriving slope, aspect, and hillshade.

- Program: <https://www.usgs.gov/3d-elevation-program>
- Data access via The National Map (TNM): <https://apps.nationalmap.gov/downloader/>
- TNM REST API: <https://tnmaccess.nationalmap.gov/api/v1/docs>

**Resolution targets:**

- **1/3 arc-second (~10 m)** — CONUS coverage, the Whumpf baseline.
- **1 meter** — partial coverage; available in many popular skiing areas
  (Colorado Rockies, Sierra, Wasatch), not all. Use where available for
  dramatically better slope-angle shading, fall back to 10 m elsewhere.

**Access pattern:** TNM's REST API returns download URLs for tiled
GeoTIFFs. The pipeline in `data/pipelines/` should:

1. Query TNM for a bounding box.
2. Download the source DEMs to `dem-raw/` in MinIO.
3. Mosaic, reproject to EPSG:3857 (or WebMercator for tiling), compute
   derivatives (slope, aspect, hillshade), and write COGs to `dem-cogs/`.

**Licensing:** U.S. federal government data, public domain.

**Update frequency:** annual-ish; Whumpf only needs to refresh on demand.

---

## Phase 2 feeds

### UAC (Utah Avalanche Center) and other regional centers

Avalanche.org maintains a zone map for the whole country with a JSON feed
of danger ratings. Individual centers (UAC, Sawtooth, NWAC, SAC, etc.)
each publish forecasts in their own format, with varying degrees of API
friendliness.

- Avalanche.org: <https://avalanche.org/> — national aggregator with a
  CAP-style feed.

Treat this the same way as CAIC: ingest polygons once, poll ratings daily.

### Strava

**Use:** import user activities (GPX streams) and overlay them on the map.

- API docs: <https://developers.strava.com/>
- Auth: OAuth 2 with PKCE.
- Rate limit: 100 requests / 15 minutes, 1000 / day (for non-partner apps).

**Gotcha:** Strava's API terms prohibit redistribution of activity data,
so this feature is strictly per-user personal use. That aligns fine with
the Whumpf single-user-first design.

---

## Phase 3 feeds (OSINT — deferred)

Not implemented yet; listed for completeness:

- **ADS-B exchange** — live aircraft positions, useful for search-and-rescue
  and heli-ski operator tracking. <https://www.adsbexchange.com/>
- **Celestrak TLEs** — satellite orbital elements, for Sentinel-1 pass
  prediction. <https://celestrak.org/>
- **Sentinel-1 SAR** — ESA C-band radar, useful for post-storm slide
  detection through cloud cover. <https://scihub.copernicus.eu/> (migrating
  to the Copernicus Data Space Ecosystem).

---

## Tables in PostGIS

This is the target schema for ingested data — actual migrations live in
`backend/app/models/` and will be created via Alembic.

| Table                 | Primary geometry       | Source   | Refresh    |
|-----------------------|------------------------|----------|------------|
| `caic_zones`          | `MULTIPOLYGON(4326)`   | CAIC     | seasonal   |
| `caic_forecasts`      | —                      | CAIC     | twice/day  |
| `snotel_stations`     | `POINT(4326)`          | NRCS     | seasonal   |
| `snotel_observations` | —                      | NRCS     | hourly     |
| `user_activities`     | `LINESTRING(4326)`     | Strava   | per-import |
| `user_lines`          | `LINESTRING(4326)`     | user     | on demand  |
| `simulation_runs`     | `LINESTRING(4326)`     | internal | on demand  |

Raster layers (DEMs and derivatives) are **not** stored in PostGIS —
they live in MinIO as COGs and are served dynamically by TiTiler.
