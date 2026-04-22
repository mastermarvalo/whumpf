# Whumpf — Project Stack & Architecture

## Project Overview

**Whumpf** is a self-hosted backcountry terrain intelligence and trip planning application. Think personal CalTopo + OnX, purpose-built for backcountry skiing and avalanche terrain analysis.

### Core Goals
- Personal mapping app for backcountry skiing and avalanche terrain
- Strava activity import and map overlay
- Live avalanche and weather condition overlays
- User-drawn line → snow runoff / avalanche runout simulation
- Multi-user ready (starts single-user)
- Fully self-hosted on UGREEN DXP4800 Plus NAS (`n3rvnas`)

### Host Environment
- **NAS:** UGREEN DXP4800 Plus, 64GB RAM, ~4TB storage on `/volume1`
- **VM:** Ubuntu 24.04 LTS, 6 CPUs, 16GB RAM, 100GB disk
- **Runtime:** Podman (podman-docker compat)
- **Reverse proxy:** Nginx Proxy Manager (already on NAS)

---

## Stack Decisions

### Frontend — Map Rendering

**Choice: MapLibre GL JS + Deck.gl overlays**

| Option | Pros | Cons |
|--------|------|------|
| **MapLibre GL JS** ✅ | Open source, GPU-rendered, self-hostable tiles, custom styling | Steeper learning curve |
| Leaflet | Simple, huge ecosystem, easy to prototype | No GPU rendering, sluggish with heavy layers |
| Deck.gl (as overlay) ✅ | Exceptional for data viz layers (heatmaps, flow sim) | Overkill as base map |

**Rationale:** MapLibre for the base map (vector tiles from Martin, raster from TiTiler), Deck.gl layered on top for runoff simulation visualization and heavy avy data layers. This is the same pattern CalTopo and OnX use.

---

### Backend — API Framework

**Choice: FastAPI (Python)**

| Option | Pros | Cons |
|--------|------|------|
| **FastAPI** ✅ | Async, fast, best geospatial ecosystem (shapely, rasterio, pyproj), auto OpenAPI docs | Python runtime overhead |
| Go (chi/gin) | Extremely fast, low memory | Geospatial ecosystem is thin |
| Node + Express | Easy JS fullstack | Geo libraries weak vs Python |

**Rationale:** Python's geospatial stack (rasterio, shapely, pyproj, numpy, pysheds, richdem) is unmatched for slope analysis, DEM processing, SNOTEL ingestion, and flow routing. FastAPI's async model handles concurrent tile/data requests cleanly.

---

### Database

**Choice: PostGIS**

| Option | Pros | Cons |
|--------|------|------|
| **PostGIS** ✅ | Industry standard, full `ST_` functions, spatial indexing | Heavier than SQLite |
| SpatiaLite | SQLite with geo extensions, dead simple | Not ideal for concurrent writes |
| MongoDB | Flexible schema, native GeoJSON | Weak for complex spatial queries |

**Rationale:** Runoff simulations, slope zone queries, Strava overlay intersection, and CAIC zone lookups all require proper spatial indexing and spatial SQL. Nothing else comes close.

---

### Raster Tile Server (DEMs, slope, hillshade, aspect)

**Choice: TiTiler**

| Option | Pros | Cons |
|--------|------|------|
| **TiTiler** ✅ | Purpose-built for COGs, FastAPI-native, dynamic rendering, S3 support | Requires COG pre-processing |
| GeoServer | Enterprise feature set, WMS/WFS | Heavy Java stack, overkill |
| pg_tileserv | Direct PostGIS → tiles | Raster support is limited |

**Rationale:** Serves slope/aspect/hillshade COGs directly from MinIO. Pairs natively with FastAPI. Handles dynamic colormaps and band math for on-the-fly visualization.

---

### Vector Tile Server (routes, zones, polygons)

**Choice: Martin**

| Option | Pros | Cons |
|--------|------|------|
| **Martin** ✅ | Rust-based, fast, PostGIS-native, near-zero config | Smaller community than alternatives |
| pg_tileserv | Simple PostGIS → MVT, lightweight | Less feature-rich |
| GeoServer | Full WFS/WMS/WMTS | Java, heavy |

**Rationale:** CAIC zones, Strava routes, user-drawn lines, and simulation outputs are all vector data. Martin serves them straight from PostGIS tables as MVT with almost zero configuration.

---

### Object Storage

**Choice: MinIO**

| Option | Pros | Cons |
|--------|------|------|
| **MinIO** ✅ | S3-compatible, self-hosted, TiTiler reads natively | Extra container |
| Local filesystem | Dead simple | TiTiler needs `file://` paths, messier |
| NAS SMB share | Uses existing NAS storage | Mount complexity in rootless containers |

**Rationale:** TiTiler has native S3 support, so it reads COGs directly from MinIO without path wrangling. Clean separation of data from compute. Buckets map to data categories (DEMs, derived rasters, user uploads).

---

### Authentication

**Choice: FastAPI JWT → Authentik later**

| Option | Pros | Cons |
|--------|------|------|
| **FastAPI JWT** ✅ (now) | Lightweight, no extra service, fast to ship | Build everything yourself |
| **Authentik** ✅ (later) | Self-hosted SSO, OIDC/SAML, great UI | Complex initial config |
| Keycloak | Industry standard, powerful | Very heavy, slow startup |

**Rationale:** Single-user for now — JWT is enough. Migrate to Authentik when adding multi-user support (friends, trip sharing, private social layer).

---

### Runoff / Avalanche Simulation

**Choice: pysheds / richdem integrated into FastAPI**

| Option | Pros | Cons |
|--------|------|------|
| **pysheds / richdem** ✅ | Lightweight, in-process, good for avy runout | Simplified hydro model |
| QGIS via Python (GRASS r.sim.water) | Real hydrological simulation | Slow, not real-time |
| Alpha angle + flow path (custom) | Avalanche-specific, CAIC methodology | Full custom build |

**Rationale:** User draws a line or selects a start point → FastAPI endpoint computes D8/D-infinity flow routing on the DEM → returns path geometry + alpha angle + runout estimate → MapLibre renders it as a vector overlay. Achievable and genuinely unique.

---

## Stack Summary

| Layer | Technology |
|-------|-----------|
| Frontend framework | Vite + React + TypeScript |
| Styling | Tailwind + shadcn/ui |
| State | Zustand |
| Data fetching | TanStack Query |
| Map base | MapLibre GL JS |
| Map overlays | Deck.gl |
| Backend | FastAPI (Python 3.12) |
| Database | PostGIS 16 |
| Raster tiles | TiTiler |
| Vector tiles | Martin |
| Object storage | MinIO |
| Auth (phase 1) | FastAPI JWT |
| Auth (phase 2) | Authentik |
| Flow simulation | pysheds / richdem |
| Container runtime | Podman (rootful or rootless) |
| Reverse proxy | Nginx Proxy Manager |

---

## Priority Data Feeds

### Phase 1
- **CAIC forecast zones** (Colorado Avalanche Information Center) — polygon zones, daily danger ratings
- **SNOTEL** — snowpack, SWE, precipitation (NRCS)
- **NOAA NDFD** — gridded forecast data
- **USGS 3DEP** — 1/3 arc-second DEMs for CONUS (slope/aspect/hillshade derivatives)

### Phase 2
- **UAC regional centers** — Utah, Sawtooth, etc.
- **Strava activity import** (already designed)

### Phase 3 (OSINT — deferred)
- ADS-B exchange
- Celestrak TLEs
- Sentinel-1 SAR

---

## Repository Layout (proposed)

```
whumpf/
├── compose.yml                    # Full stack compose file
├── .env                           # Secrets (gitignored)
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                # FastAPI entrypoint
│   │   ├── config.py
│   │   ├── db.py                  # PostGIS connection
│   │   ├── auth/                  # JWT auth
│   │   ├── routers/               # API route modules
│   │   │   ├── zones.py           # CAIC zones
│   │   │   ├── snotel.py
│   │   │   ├── simulation.py      # Runoff endpoint
│   │   │   ├── strava.py
│   │   │   └── tiles.py
│   │   ├── services/              # Business logic
│   │   ├── models/                # SQLAlchemy + Pydantic
│   │   └── simulation/            # pysheds/richdem logic
│   └── tests/
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── Map.tsx
│   │   │   ├── LayerPanel.tsx
│   │   │   └── ...
│   │   ├── hooks/
│   │   ├── stores/                # Zustand
│   │   ├── api/                   # TanStack Query hooks
│   │   └── styles/
│   └── public/
├── data/
│   ├── pipelines/                 # DEM → COG processing scripts
│   └── ingest/                    # Feed sync jobs (CAIC, SNOTEL)
├── martin/
│   └── config.yml
├── titiler/
│   └── config/
└── docs/
    ├── architecture.md
    ├── data-sources.md
    └── runbook.md
```

---

## Build Phases

### Phase 0 — Infrastructure
- [ ] Compose stack: PostGIS, MinIO, TiTiler, Martin, FastAPI skeleton
- [ ] Network, volumes, healthchecks configured
- [ ] NPM routing for all services
- [ ] `.env` pattern established

### Phase 1 — Base Map
- [ ] FastAPI health endpoint
- [ ] PostGIS extensions loaded (`postgis`, `postgis_raster`)
- [ ] React + MapLibre renders a map (OSM tiles as baseline)
- [ ] Custom basemap style

### Phase 2 — DEM Pipeline
- [ ] 3DEP download script (Colorado first, CONUS later)
- [ ] Mosaic → reproject → derivatives (slope, aspect, hillshade) → COG
- [ ] Upload to MinIO
- [ ] TiTiler serves COGs, visible in MapLibre

### Phase 3 — Vector Data
- [ ] CAIC zone ingest → PostGIS
- [ ] Martin serves zones as MVT
- [ ] Danger rating styled on map
- [ ] SNOTEL station points + live data

### Phase 4 — Auth & User Data
- [ ] FastAPI JWT auth
- [ ] User model
- [ ] Strava OAuth + activity import → PostGIS
- [ ] Activity overlay on map

### Phase 5 — Simulation
- [ ] Draw-line frontend tool
- [ ] FastAPI runoff endpoint (pysheds/richdem)
- [ ] Result rendered via Deck.gl PathLayer
- [ ] Alpha angle + runout estimate

### Phase 6 — Multi-user
- [ ] Migrate auth to Authentik
- [ ] Trip sharing
- [ ] Private social layer

---

## Naming & Conventions

- Container names: `whumpf-<service>` (e.g., `whumpf-postgis`, `whumpf-api`)
- Network: `whumpf-net`
- Volumes: `whumpf-<purpose>` (e.g., `whumpf-pgdata`, `whumpf-minio`)
- DB: database `whumpf`, user `whumpf`
- MinIO buckets: `dem-raw`, `dem-cogs`, `user-uploads`, `strava-cache`
- FQDN pattern (via NPM): `<service>.whumpf.<your-domain>`

---

## Key References

- **MapLibre GL JS:** https://maplibre.org/
- **Deck.gl:** https://deck.gl/
- **FastAPI:** https://fastapi.tiangolo.com/
- **PostGIS:** https://postgis.net/
- **TiTiler:** https://developmentseed.org/titiler/
- **Martin:** https://maplibre.org/martin/
- **MinIO:** https://min.io/
- **Authentik:** https://goauthentik.io/
- **pysheds:** https://github.com/mdbartos/pysheds
- **richdem:** https://richdem.readthedocs.io/
- **USGS 3DEP:** https://www.usgs.gov/3d-elevation-program
- **CAIC:** https://avalanche.state.co.us/
