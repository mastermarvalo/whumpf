# Whumpf architecture

This doc is the "big picture" reference. For the per-component rationale
(why PostGIS instead of SpatiaLite, why FastAPI instead of Go) see
[`whumpf-stack.md`](../whumpf-stack.md). This file is the wiring diagram.

## Request flows

### Base map render

```
browser ──► MapLibre ──► fetch style.json (from frontend bundle)
                    │
                    ├──► Martin         :3000/{source}/{z}/{x}/{y}.mvt   (future: PostGIS vector — CAIC zones, routes)
                    │
                    ├──► TiTiler        :8001/cog/tiles/{z}/{x}/{y}.png
                    │      └──► MinIO → dem-cogs/{region}/hillshade.tif  (slope, aspect, hillshade overlays)
                    │                   via vsicurl HTTP + GDAL/rasterio
                    │
                    └──► FastAPI        :8000/tiles/*
                           ├── /terrain_rgb/{z}/{x}/{y}   → 10m DEM; 1m hires w/ 10m fallback at z≥13
                           ├── /contours/{z}/{x}/{y}      → zoom-adaptive contour lines from DEM COGs
                           ├── /slope/{z}/{x}/{y}         → CalTopo-style slope angle colormap
                           └── /terrain_filter/{z}/{x}/{y}→ aspect+slope filter overlay
                                all read dem.tif / dem_hires.tif from MinIO via vsicurl
```

MapLibre pulls tiles from three upstreams. TiTiler handles hillshade and
other direct COG renders. The FastAPI `/tiles` router handles terrain-rgb,
contours, slope, and the terrain filter — these need custom rendering logic
(terrarium encoding, contourpy, CalTopo colormap) that TiTiler can't do
natively. Martin is wired up but not yet serving production vector data
(pending CAIC zone ingest and Strava activity import to PostGIS).

### User action (e.g., "draw line → simulate runout")

```
browser ──► POST /api/simulation/runout { line: GeoJSON }
              └──► FastAPI
                     ├──► read DEM COG from MinIO into memory (rasterio)
                     ├──► pysheds / richdem flow routing
                     ├──► write result path into PostGIS
                     └──► return { path: GeoJSON, alpha_angle, runout_m }

browser ──► MapLibre adds result as a Deck.gl PathLayer overlay
```

### Auth (Phase 4)

```
browser ──► POST /api/auth/login { email, password }
              └──► FastAPI → bcrypt verify → sign JWT → return token
browser ──► subsequent requests carry "Authorization: Bearer <token>"
```

## Networks and exposure

```
                ┌───────────────────────────────────────────────┐
                │  NAS host (n3rvnas)                           │
                │                                               │
                │  ┌─────────────────┐                          │
                │  │ Nginx Proxy Mgr │ ◄── public HTTPS         │
                │  └────────┬────────┘                          │
                │           │                                   │
                │  ┌────────▼─────────────────────────────────┐ │
                │  │ Ubuntu 24.04 VM                          │ │
                │  │                                          │ │
                │  │  ┌───────────────── whumpf-net ────────┐ │ │
                │  │  │                                    │ │ │
                │  │  │  frontend  api  martin  titiler    │ │ │
                │  │  │     │       │      │       │       │ │ │
                │  │  │     └───┬───┴──────┼───────┘       │ │ │
                │  │  │         │          │               │ │ │
                │  │  │      postgis     minio             │ │ │
                │  │  │                                    │ │ │
                │  │  └────────────────────────────────────┘ │ │
                │  └──────────────────────────────────────────┘ │
                └───────────────────────────────────────────────┘
```

Only NPM is publicly exposed. Every backend service listens on the VM's
loopback (`127.0.0.1:PORT`) or on the internal compose network.

## Data storage

- **PostGIS** (volume `whumpf-pgdata`) — user accounts, activities, CAIC
  zones, SNOTEL stations, simulation results, drawn lines, trip plans.
  Everything vector and everything transactional.
- **MinIO** (volume `whumpf-minio`) — raster data that doesn't belong in a
  relational DB.
  - `dem-raw/` — original 3DEP downloads before processing
  - `dem-cogs/` — cloud-optimized GeoTIFFs (slope, aspect, hillshade, DEM)
  - `user-uploads/` — GPX files, photos
  - `strava-cache/` — cached Strava API responses (stream data)

## Why two tile servers?

Martin and TiTiler solve different problems.

**Martin** serves vector tiles straight from PostGIS tables as Mapbox
Vector Tile (MVT). This is optimal for everything polygon/line/point:
CAIC forecast zones, Strava routes, drawn lines, simulation outputs. The
client can style these freely with the MapLibre style spec, and changes to
the DB are picked up automatically.

**TiTiler** serves raster tiles dynamically from Cloud Optimized GeoTIFFs.
It reads COGs directly from S3/MinIO using GDAL's range-request support,
and renders PNG/WebP tiles on the fly with configurable colormaps. This is
optimal for hillshade, slope angle overlays, aspect shading, and any other
continuous raster data.

Mixing them in one MapLibre style is standard practice — it's what CalTopo,
OnX, and Gaia all do under the hood.

## Phase roadmap

Mirrored from `whumpf-stack.md`:

| Phase | Focus                                   | Key deliverable              | Status |
|-------|-----------------------------------------|------------------------------|--------|
| 0     | Infrastructure                          | Stack boots, healthchecks    | ✅ done |
| 1     | Base map renders                        | MapLibre + 3D terrain        | ✅ done |
| 2     | DEM pipeline                            | Slope/terrain/contours live  | ✅ done |
| 3     | Vector data feeds                       | CAIC zones styled on map     | 🔄 partial (SNOTEL done; CAIC pending) |
| 4     | Auth + Strava                           | Activity overlays            | 🔄 partial (auth + import done; map overlay pending) |
| 5     | Simulation                              | Draw line → runout path      | ⬜ not started |
| 6     | Multi-user                              | Authentik, trip sharing      | ⬜ not started |
