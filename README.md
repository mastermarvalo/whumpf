# Whumpf

Self-hosted backcountry terrain intelligence and trip planning.
Personal CalTopo + OnX, purpose-built for backcountry skiing and avalanche
terrain analysis.

See [`whumpf-stack.md`](./whumpf-stack.md) for the full stack decisions and
rationale. This README covers how to actually run the thing.

## Services at a glance

| Service     | Image                                     | Host port                | Purpose                              |
|-------------|-------------------------------------------|--------------------------|--------------------------------------|
| `postgis`   | `postgis/postgis:17-3.5-alpine`           | `127.0.0.1:5432`         | Spatial database                     |
| `minio`     | `quay.io/minio/minio:latest`              | `9000` (S3), `9001` (UI) | Object storage for DEM COGs, uploads |
| `titiler`   | `ghcr.io/developmentseed/titiler:latest`  | `127.0.0.1:8001`         | Dynamic raster tile server           |
| `martin`    | `ghcr.io/maplibre/martin:v0.18.0`         | `127.0.0.1:3000`         | PostGIS → vector tiles (MVT)         |
| `api`       | local build (`whumpf-api:dev`)            | `127.0.0.1:8000`         | FastAPI backend                      |
| `frontend`  | local build (`whumpf-frontend:dev`)       | `5173`                   | Vite + React + MapLibre              |

Ports starting with `127.0.0.1:` are host-bound to localhost only; these should
be reverse-proxied via Nginx Proxy Manager on the NAS, not exposed directly.

## First-time setup

Full install steps are in [`docs/runbook.md`](./docs/runbook.md). The short
version:

1. Install Podman on the Ubuntu 24.04 VM, plus the Docker CLI shim and compose
   plugin. See the runbook — the version in Ubuntu's default repos is too
   old and needs workarounds.
2. Clone this repo, then:

   ```bash
   cp .env.example .env
   # edit .env and set real passwords + a JWT secret
   #   openssl rand -hex 32     <- use this for JWT_SECRET
   make up
   ```
3. Verify health:

   ```bash
   make health
   ```

   You should see `"ready": true` with a `postgis` version string.

4. Open the frontend at <http://localhost:5173>. You should see a dark map of
   the San Juans with a green `whumpf 0.1.0 · dev` pill in the bottom-right.

## Daily commands

```bash
make up            # bring up everything (builds images if needed)
make down          # stop everything (keeps volumes)
make logs          # tail all services
make logs-api      # tail just the API
make psql          # jump into the DB
make health        # curl /readyz
make test-api      # run backend tests
make nuke          # DESTROYS all volumes — fresh start
```

## Build phase status

- [x] **Phase 0** — infrastructure: compose stack, networks, volumes, healthchecks, MinIO bucket bootstrap
- [ ] Phase 1 — base map + PostGIS extensions verified + FastAPI health
- [ ] Phase 2 — DEM pipeline (3DEP → COG → MinIO → TiTiler)
- [ ] Phase 3 — vector data (CAIC zones, SNOTEL) via Martin
- [ ] Phase 4 — auth + Strava import
- [ ] Phase 5 — runoff / avalanche simulation
- [ ] Phase 6 — multi-user (Authentik)

## Layout

```
whumpf/
├── compose.yml                  # full stack
├── .env.example                 # env template
├── Makefile                     # daily commands
├── whumpf-stack.md              # architecture + decisions
├── backend/                     # FastAPI
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── app/
├── frontend/                    # Vite + React + MapLibre
│   ├── Dockerfile
│   ├── package.json
│   └── src/
├── scripts/
│   └── postgis-init.sql         # runs once, on first DB boot
├── data/
│   ├── pipelines/               # DEM → COG scripts
│   └── ingest/                  # CAIC / SNOTEL feed jobs
└── docs/
    ├── runbook.md               # install, troubleshooting, tear-down
    ├── architecture.md          # stack decisions (mirrors whumpf-stack.md)
    └── data-sources.md          # feeds, schemas, licensing
```
# whumpf
