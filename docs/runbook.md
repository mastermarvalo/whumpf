# Whumpf runbook

Operational reference for the Ubuntu 24.04 VM on the UGREEN DXP4800 Plus NAS
(`n3rvnas`). Covers install, first boot, tear-down, and troubleshooting.

## Host environment assumed

- UGREEN DXP4800 Plus, 64 GB RAM, ~4 TB on `/volume1`
- Ubuntu 24.04 LTS VM: 6 CPU, 16 GB RAM, 100 GB disk
- Nginx Proxy Manager already running on the NAS (handles TLS and routing)

## 1. Install Podman + Docker compatibility layer

**Important caveat:** the `podman` package in Ubuntu 24.04's default repos is
4.9.3, which is too old for clean healthcheck-based `depends_on` with compose
v2. If you run into problems, grab a newer Podman from the Kubic project or
build from source. For most of what Whumpf needs, 4.9.3 works.

```bash
sudo apt update
sudo apt install -y podman podman-docker uidmap slirp4netns fuse-overlayfs
```

`podman-docker` installs a `docker` shim that points at Podman, so the Docker
CLI and Docker Compose v2 work transparently against Podman's API. On the
VM itself `docker` is only the podman shim and has no `compose` subcommand;
the Makefile calls the compose v2 plugin binary directly,
`~/.docker/cli-plugins/docker-compose`.

Enable the Podman user socket so Docker Compose v2 can talk to it:

```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock
# make it stick:
echo 'export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock' >> ~/.bashrc
```

Install Docker Compose v2 (the plugin binary) so `docker compose` works:

```bash
mkdir -p ~/.docker/cli-plugins
curl -fSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o ~/.docker/cli-plugins/docker-compose
chmod +x ~/.docker/cli-plugins/docker-compose
docker compose version   # should print "Docker Compose version v2.x.x"
```

Confirm user namespaces for rootless:

```bash
grep "^$(whoami):" /etc/subuid /etc/subgid
# If nothing prints, add your user:
#   sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(whoami)
#   podman system migrate
```

### Alternative: use `podman-compose`

If you'd rather skip the Docker shim, `podman-compose` also works:

```bash
sudo apt install -y podman-compose
make up COMPOSE=podman-compose
```

The trade-off: `podman-compose` has thinner support for compose-spec features
(notably `depends_on: condition: service_healthy`). With Whumpf you may see
the `api` container start before Postgres is ready and restart a few times
before coming up; docker-compose-on-Podman handles this cleanly.

## 2. Clone the repo and configure

```bash
git clone <your-repo-url> whumpf
cd whumpf
cp .env.example .env
```

Edit `.env` and set:

- `POSTGRES_PASSWORD` — any strong string
- `MINIO_ROOT_USER` — at least 3 characters
- `MINIO_ROOT_PASSWORD` — at least 8 characters
- `JWT_SECRET` — generate with `openssl rand -hex 32`
- `REDIS_PASSWORD` — generate with `openssl rand -hex 32` (hex only; it is
  embedded in the api's `REDIS_URL`)

All of these are required; the compose file will refuse to start without them.

### Settings added in Phase 2

As they appear in `.env.example`:

- `REDIS_PASSWORD` — Redis listens on 127.0.0.1 only and requires this
  password. compose builds the api's `REDIS_URL` from it. Hex only.
- `REFRESH_TOKEN_TTL_DAYS` (default 60) — how long a refresh token stays
  valid. Each refresh issues a new one, so active users stay signed in.
- `TILES_ALLOW_TOKENLESS` (default true) — lets `/tiles/*` requests through
  without a `?t=` tile token (from `POST /tiles/token`), under a strict
  per-IP limit. **Turn off when the old site is retired**, then purge
  `/tiles/*` in Cloudflare or cached tokenless URLs keep being served.
- `STRAVA_RETURN_TO_ALLOW_DEV` (default false) — also allows the Expo dev
  `return_to` targets `exp://...` and `http://localhost:8081/...` for Strava
  connect. **Turn off when the old site is retired** (and keep it false in
  prod).
- `LEGACY_EMAIL_LINKS` (default true) — verify/reset emails also carry the
  old `?verify=` / `?reset=` links, and the API accepts them. Set false once
  the old website is gone.

## 3. First boot

```bash
make up
```

What happens, in order:

1. `postgis` starts. The `scripts/postgis-init.sql` script runs on first boot
   only, creating the `postgis`, `postgis_raster`, `postgis_topology`,
   `pg_trgm`, and `btree_gist` extensions.
2. `minio` starts. The volume is empty.
3. `minio-init` runs `mc mb` to create `dem-raw`, `dem-cogs`, `user-uploads`,
   and `strava-cache` buckets, then exits. It is a one-shot.
4. `titiler` and `martin` start and point at MinIO and Postgres respectively.
5. `api` builds from `backend/` and starts.
6. `frontend` builds from `frontend/` and starts Vite.

Check everything is up:

```bash
make ps
make health
```

`make health` should return `"ready": true` with a PostGIS version string.
If not, see Troubleshooting below.

## 4. Reverse proxy (Nginx Proxy Manager)

From the NAS's NPM instance, point your domains at the VM's IP:

| Domain                    | Upstream                | Purpose           |
|---------------------------|-------------------------|-------------------|
| `whumpf.<your-domain>`    | `http://<vm-ip>:5173`   | Frontend          |
| `api.whumpf.<your-domain>`| `http://<vm-ip>:8000`   | Backend API       |
| `tiles.whumpf.<your-domain>` | `http://<vm-ip>:3000` | Martin (MVT)      |
| `rasters.whumpf.<your-domain>` | `http://<vm-ip>:8001` | TiTiler        |

### Cloudflare cache

`api.whumpf.co` is proxied by Cloudflare. A Cache Rule makes
`https://api.whumpf.co/tiles/*` Eligible for cache (tile URLs have no file
extension, so Cloudflare would not cache them by default). Cloudflare follows
the API's `Cache-Control` (a day for most tiles). The rule's Browser TTL is
set to respect origin: otherwise Cloudflare raises any `max-age` under 4 h to
`max-age=14400`, and browsers keep radar meta (60 s) for 4 hours. Check it with:

```bash
curl -sI "https://api.whumpf.co/tiles/slope/10/210/394?region=colorado" | grep -i cf-cache-status
# first request MISS, then HIT; DYNAMIC means the rule is not applying
curl -sI "https://api.whumpf.co/tiles/radar/meta?region=colorado" | grep -i cache-control
# max-age=60; max-age=14400 means Browser TTL is overriding the origin again
```

Each tile token (`?t=`) gets its own cache entry: removing a single query
parameter from the cache key needs Enterprise (docs/PLAN.md, Tile access).
After changing what a tile endpoint returns, purge `/tiles/*`. Snowfall,
snow depth and temperature tiles carry their data revision in the URL, and
radar tiles their frame time (§8, §9), so new weather data needs no purge.

### Loopback-only services

Because every container uses host networking, ports are bound by each service
itself. PostGIS (5432), Redis (6379, password required), MinIO (9000 S3 and
9001 console) and Martin (3000) listen on `127.0.0.1` only, so they are
unreachable from the LAN and from Nginx Proxy Manager. The api, frontend and
TiTiler still listen on all interfaces (restrict them with `ufw`); TiTiler
stays that way until the old website, which calls it directly, is gone.

This means NPM can no longer proxy `tiles.` (Martin) or `minio.` (console)
hosts. To reach the MinIO console, Postgres or Redis from your workstation,
use an SSH tunnel:

```bash
ssh -L 9001:127.0.0.1:9001 -L 5432:127.0.0.1:5432 <user>@<vm-ip>
# console at http://localhost:9001; psql -h localhost works
```

Verify from another machine on the LAN that these ports are closed:
`nc -zv <vm-ip> 5432 6379 9000 9001 3000` should fail for all of them. On the
VM, `ss -tlnp | grep -E ':(5432|6379|9000|9001|3000)\b'` should show only
`127.0.0.1`.

Remember to update `.env` so `CORS_ALLOW_ORIGINS` and the `VITE_*` URLs match
the reverse-proxied origins, then `make restart`.

### Strava connect: where users land afterwards

Strava always redirects to the API's `STRAVA_REDIRECT_URI`
(`/auth/strava/callback`), so the Strava app's "Authorization Callback
Domain" stays the API host. The API then sends the user on:

- If the client passed `?return_to=` to `GET /auth/strava/authorize`, the
  callback redirects there with `?strava=connected` or
  `?strava=error&reason=denied|expired|bad_state|token_exchange_failed`.
  Allowed targets: `whumpf://...` and `https://whumpf.co/...`. With
  `STRAVA_RETURN_TO_ALLOW_DEV=true` (dev only, keep `false` in prod),
  `exp://...` and `http://localhost:8081/...` are also allowed. Anything else
  is a 400.
- Without `return_to` (the old website), it redirects to `STRAVA_SUCCESS_URL`
  with `?strava=connected|denied|error`, as before.

`return_to` travels inside the OAuth `state`, a JWT signed with `JWT_SECRET`
that is valid for 10 minutes. A forged, tampered or wrong-purpose state is a
400 with no redirect. Rotating `JWT_SECRET` breaks any connects in progress.

## 5. Tear-down

```bash
make down            # stop containers, keep data
make nuke            # stop containers AND delete all volumes — destructive
```

`make nuke` wipes Postgres data and every MinIO bucket. The raw DEM files
themselves (typically several GB) live in the `dem-raw` bucket and would need
to be re-downloaded from 3DEP.

## 6. Schema migrations (Alembic)

The schema is owned by Alembic (`backend/migrations/`). The api container's
`entrypoint.sh` runs `alembic upgrade head` once before uvicorn starts, so a
normal deploy (`make up`) applies pending migrations by itself. If a
migration fails, the api doesn't start (Postgres DDL is transactional, so a
failed migration leaves no half-applied schema); `make logs-api` shows why.

`make migrate`, `make alembic`, `make db-check`, `make schema-diff` and
`make test-api` all run in a **one-off container from the current api image**
(`compose run --rm`), never by exec'ing into the running api. After
`make build-api` they use the new code while the old api keeps serving.

### Changing the schema

```bash
# 1. edit the model in backend/app/models/
make makemigration m="add foo to trips"   # writes backend/migrations/versions/<rev>_add_foo_to_trips.py
# 2. read the generated file; fix anything autogenerate got wrong
#    (renames show up as drop+add; data backfills must be written by hand)
make test-api      # builds whumpf_test from scratch and fails if models ≠ migrations
make up            # deploy: entrypoint applies it
make health        # checks.schema.ok must be true
```

Useful: `make alembic ARGS=current`, `make alembic ARGS=history`,
`make db-check` (models vs. live DB), `make schema-diff` (live DB vs. a
scratch DB built from migrations).

### Safety rails

- **Tests never touch the live DB.** `tests/conftest.py` drops and recreates
  `whumpf_test` (same server) and refuses to drop/create any other name.
- **Baseline downgrade is blocked.** `0001_baseline` downgrade drops every
  table, so it refuses unless the DB is `whumpf_test` or you pass
  `ALLOW_DESTRUCTIVE_DOWNGRADE=1` on that one make command line. It's not read
  from `.env`; never set it there.
- **Unstamped databases are not upgraded.** If tables exist but there is no
  `alembic_version` row, `0001_baseline` upgrade refuses instead of trying to
  create tables over them.

### Backups and restore

```bash
make backup                      # local db-dump + off-site dump (scripts/backup.sh, BACKUP_S3_*)
make db-dump                     # local pg_dump -Fc → /data/backups/whumpf-<utc>.dump
```

`make backup` runs daily from cron at 03:00 server time
(docs/external-services.md, "Schedule daily"): a local `db-dump` (copy out
of the container checked by sha256; dumps older than 14 days deleted), then
the same file uploaded to Cloudflare R2 (`whumpf-backups/postgres/`, 30-day
retention) and read back by sha256, then `user-uploads` mirrored. It logs to
`/data/backups/backup.log` and ends with a `complete` line. If the local dump
fails, nothing goes up for postgres, uploads are still mirrored, and it exits 1.
An off-site dump restores the same way as a local one (download it, then
`make db-restore`).

`db-dump` writes inside the postgis container, checks the file with
`pg_restore --list`, then copies it out (no streaming through `exec`, which
podman occasionally cuts off with "connection reset by peer"). A restore of
one of these into a throwaway database was rehearsed on 2026-10-14; row
counts matched.

Restore a local dump (overwrites the live DB; stop the api and the pack
worker first so nothing writes during the restore):

```bash
make stop-api stop-pack-worker
make db-restore FILE=/data/backups/whumpf-<utc>.dump CONFIRM=yes
make up
```

### One-time cutover: stamping the existing prod database

Done: prod is stamped at `0001_baseline`. Kept as the record of the steps.

Prod's schema was built by the old `create_all()` + `_SCHEMA_PATCHES` and has
no `alembic_version` table. It must be **stamped** at `0001_baseline` (marks
it as already migrated; writes only the `alembic_version` row) before the new
api starts. Verified on 2026-10-14: `make schema-diff` showed the live schema
and a migrations-built schema identical (333 lines).

Before deploy (old api keeps serving throughout):

```bash
cd ~/whumpf
git log --oneline -1                  # the Alembic commit is checked out
make backup                           # local db-dump + off-site
make db-dump                          # local; prints "tables with data: N" and the file
make psql                             # select count(*) from users; … routes; … trips;  (note them)
make build-api                        # new image; running api untouched
make schema-diff                      # must end with "✓ schema identical"
make alembic ARGS="stamp 0001_baseline"
make alembic ARGS=current             # 0001_baseline (head)
make db-check                         # "No new upgrade operations detected."
```

If `schema-diff` or `db-check` fails: `make alembic ARGS="stamp base"`
(removes the version row again — nothing else was changed) and don't deploy.

Deploy and after:

```bash
make up
make logs-api       # no "Running upgrade" lines (already at head); uvicorn starts
make health         # "ready": true and checks.schema.ok true
make test-api       # runs on whumpf_test, not prod
make psql           # same row counts as before
```

Rollback: `git checkout <previous commit> && make up`. The old code's
`create_all` + patches are no-ops on this schema and it ignores
`alembic_version`, so no database restore is needed. Only restore
(`make db-restore`) if data itself is damaged.

## 7. Ski run tiles

Ski runs, lifts and ski area outlines come from OpenStreetMap (Geofabrik
state extracts) and are rebuilt weekly into vector tiles. They replace the
OpenSnowMap raster layer.

```
Geofabrik colorado-latest.osm.pbf
  -> tilemaker (container, profile in data/pipelines/ski/)
  -> /data/ski-work/out/ski_colorado.mbtiles      validated
  -> /data/martin-tiles/ski_colorado.mbtiles      Martin source "ski_colorado"
  -> GET /tiles/ski/{z}/{x}/{y}?region=colorado   API: tile token, rate limits, Cloudflare cache
```

Attribution: "© OpenStreetMap contributors" (ODbL), linked to
<https://www.openstreetmap.org/copyright>. It is in the MBTiles metadata and
must be shown whenever the layer is on. The tiles are an ODbL Produced Work,
so attribution is all that is required, offline packs included.

### Layers and fields

The file has z8–14; clients set `maxzoom: 14` and overzoom. Every field is a
string. `osm_id` is a string because tilemaker stores numbers as float32,
which corrupts ids above 2^24.

| Layer | Geometry | Zooms | Fields |
| --- | --- | --- | --- |
| `ski_runs` | lines; polygons for runs mapped as areas | 11–14 | `piste_type` (`downhill`, `nordic`, `skitour`), `difficulty` (`novice`, `easy`, `intermediate`, `advanced`, `expert`, `freeride`, `extreme`; absent if untagged), `grooming` (OSM value, `;` normalised to `+`, e.g. `classic+skating`, `mogul`, `backcountry`), `name`, `osm_id`, `osm_type` |
| `ski_lifts` | lines | 10–14 | `lift_type` (`chair_lift`, `gondola`, `cable_car`, `mixed_lift`, `drag_lift`, `t-bar`, `j-bar`, `platter`, `rope_tow`, `magic_carpet`), `name`, `osm_id`, `osm_type` |
| `ski_areas` | polygon outline + one point for the label | 8–14 | `name`, `osm_id`, `osm_type` |

Nordic and skitour trails often get their tags from a `route=piste` relation;
member ways inherit `piste_type`, `difficulty`, `grooming` and `name` from it.
`osm_type` is `way` or `relation`. The layer and field list is enforced by
`EXPECTED_FIELDS` in `data/pipelines/ski_tiles.py`; change it, `process.lua`,
this table and the apps together.

### Make targets

| Target | What it does |
| --- | --- |
| `make ski-tiles` | fetch + build + validate. Does not touch what is served |
| `make ski-tiles-publish` | validate again, swap into `/data/martin-tiles`, restart Martin, roll back on failure |
| `make ski-tiles-refresh` | both; the weekly job |
| `make ski-tiles-fetch` / `ski-tiles-build` | the single steps |
| `make restart-martin` | restart Martin only |

All take `REGION=` (default `colorado`), `SKI_WORKDIR=` (default
`/data/ski-work`) and `SKI_TILES_DIR=` (default `/data/martin-tiles`). The
workdir must be on the same filesystem as the tiles dir so the swap is an
atomic rename; publish refuses otherwise. Needs `ogrinfo` (`gdal-bin`) and
the pipeline venv (`.venv`, `data/pipelines/requirements.txt`).

Workdir layout: `<region>/download/` (the extract and its `.md5`, re-downloaded
only when Geofabrik's md5 changes), `out/` (latest build), `prev/` (the file
served before the last publish), `<region>/published.json` (feature counts
of the served file), `refresh.log` (cron output).

### Weekly refresh

User crontab (`crontab -l`), Tuesdays 04:00 server time (Mountain), outside
ski hours and clear of the 03:00 backup:

```
XDG_RUNTIME_DIR=/run/user/1000
DOCKER_HOST=unix:///run/user/1000/podman/podman.sock
0 4 * * 2 cd /home/ronkerflonk/whumpf && flock -n /data/ski-work/.refresh.lock make ski-tiles-refresh >> /data/ski-work/refresh.log 2>&1
```

Cron has no login environment, so `DOCKER_HOST` and `XDG_RUNTIME_DIR` are
needed for `make restart-martin` to reach rootless podman. Linger must stay on
(`loginctl show-user $USER -p Linger`). A run takes about 15 seconds plus the
download.

### How the swap avoids downtime

Martin only scans `/data/martin-tiles` at startup and keeps the old file open
after a rename, so publishing needs a restart. Publish:

1. validates the build (below); a failure exits non-zero and nothing changes;
2. hard-links the served file to `prev/` (outside the tiles dir: Martin would
   serve any `.mbtiles` left in there as an extra source);
3. renames the build over the served file (atomic);
4. restarts Martin and waits up to 60 s for `ski_colorado` in `/catalog`;
   if it doesn't appear, puts `prev/` back and restarts again.

Martin is unreachable for well under a second (measured: one missed 200 ms
probe). The API's retries (`call_with_resilience`, about 0.5–3 s) cover that,
and Cloudflare and the clients hold tiles they already have. The terrain
MBTiles, also served by Martin, see the same blip.

### Validation

A build is refused if any of these fail:

- layers and field names/types differ from `EXPECTED_FIELDS`;
- attribution missing, format not `pbf`, or no tiles;
- `bounds` outside the region bbox in `data/pipelines/ski/regions.json`;
- a layer's z14 feature count is below the region's `min_features`, or less
  than half of the served file's count (`published.json`). That is more likely
  a truncated extract than real edits. If the drop is real, delete
  `published.json` and publish again.

### Rollback

```bash
ln -f /data/ski-work/prev/ski_colorado.mbtiles /data/ski-work/out/ski_colorado.mbtiles
make ski-tiles-publish REGION=colorado
```

(Publish re-validates it and keeps the bad one in `prev/`.)

To test the automatic rollback, point publish's "is Martin back" check at a
dead port. Martin really restarts twice; the run takes about 2 minutes, exits
1 with "previous file restored", and the served file's inode is unchanged
(tested Oct 14):

```bash
make ski-tiles
stat -c %i /data/martin-tiles/ski_colorado.mbtiles
.venv/bin/python data/pipelines/ski_tiles.py --region colorado publish --martin-url http://127.0.0.1:3999
stat -c %i /data/martin-tiles/ski_colorado.mbtiles   # same inode as before
```

### Adding a state

1. Add the region to `backend/app/regions.py` (the API only proxies known ids).
2. Add it to `data/pipelines/ski/regions.json`: Geofabrik path, bbox (the
   extract's PBF header bbox is unreliable, so tilemaker gets it from here),
   and `min_features` (about a quarter of a first build's counts).
3. `make ski-tiles-refresh REGION=<id>`, then add a line to the crontab.

### Bumping tilemaker

Only a `master` tag is published, so `TILEMAKER_IMAGE` in the Makefile pins a
digest. To update, pull `ghcr.io/systemed/tilemaker:master`, put its digest in
the Makefile, run `make ski-tiles` and compare the counts with
`published.json`. Known quirks of the pinned version: numeric attributes are
float32, `AttributeInteger` silently drops the field, and its own
"Generated ... lines: 0" summary line is wrong.

## 8. Recent snowfall

New snow over the last 24 h and 72 h from the NOAA NOHRSC National Snowfall
Analysis, clipped to each region and served as raster tiles. There is no NOAA
map service for snowfall (the `NOHRSC_Snow_Analysis` MapServer only has snow
depth and SWE), so the GeoTIFFs are fetched hourly.

```
https://www.nohrsc.noaa.gov/snowfall/data/YYYYMM/sfav2_CONUS_{24h,72h}_YYYYMMDDHH.tif
  -> make snowfall-refresh (hourly cron, app/jobs/snowfall_refresh.py)
       validate, clip to the region's max_bounds, write a COG
  -> MinIO dem-cogs/snowfall/<region>/<period>/<YYYYMMDDHH>-<sha8>.tif
     MinIO dem-cogs/snowfall/<region>/latest.json        what is served
  -> GET /tiles/snowfall/meta?region=colorado            TiTiler renders the tiles
     GET /tiles/snowfall/{24h|72h}/{z}/{x}/{y}?region=colorado&v=<revision>
```

### Source facts

- Issues at 00Z and 12Z (6 pm / 6 am MDT). The newest issue is shown, and
  `meta` gives its window (`start` → `valid`, UTC) so the app can always
  show which window it covers.
- NOHRSC reruns each analysis hourly for five days to pick up late reports
  and **overwrites the file in place**. The job re-checks the served issue
  every run with `If-None-Match`; a changed file becomes a new revision.
- 0.04° (~4 km) grid, EPSG:4326. The GeoTIFFs are in **inches** (the NetCDF
  twins are in metres). Nodata is -99999; the 48 h/72 h TIFs omit the NoData
  tag, so the job sets it.
- Public domain (<https://www.weather.gov/disclaimer>). Show
  "NOAA/NWS NOHRSC National Snowfall Analysis" with the valid time whenever
  the layer is on; don't imply NOAA endorses the app. `robots.txt`
  disallows `*.nc` and `*.png` but not the TIFs.

### API

All under `/tiles`, so tile tokens, rate limits and the Cloudflare rule apply.

| Endpoint | Cache-Control |
| --- | --- |
| `GET /tiles/snowfall/meta?region=` | 300 s; 60 s while a period has no data |
| `GET /tiles/snowfall/{period}/{z}/{x}/{y}?region=&v=` with the current revision | 1 day |
| same, without `v` or with an old one (serves the current data) | 15 min |
| no data yet, or MinIO unreadable | 503 `no-store` |

`meta` returns `units` (`in`), `attribution`, `attribution_url`, `bounds`,
`maxzoom` (10; overzoom past it), `legend` (`min`, `max`, `color`, `label`)
and per period `available`, `start`, `valid`, `revision`, `max_in`,
`fetched` and `tiles` (a URL template that already carries `v`; add `t`).
A new revision is a new tile URL, so Cloudflare never needs a purge for this
layer.

Colour scale, inches (one list in `app/services/snowfall.py` feeds both the
legend and TiTiler; under 0.1 in is transparent):

| 0.1–1 | 1–2 | 2–4 | 4–6 | 6–8 | 8–12 | 12–18 | 18–24 | 24–36 | 36+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `#d6e9f8` | `#a6cfee` | `#6aaee0` | `#3a86c8` | `#1f5aa8` | `#4b3a9e` | `#7b3fa6` | `#a8389a` | `#cf2f7c` | `#8c0f45` |

SNOTEL stations (`GET /snowpack/stations`) also carry `new_snow_24h_in`,
`new_snow_72h_in`, `new_swe_24h_in`, `new_swe_72h_in` and `new_snow_as_of`.
They come from SNOTEL's daily start-of-day readings (midnight local), so the
window is midnight to midnight, not NOHRSC's 12Z one. Only rises count
(settling isn't negative snow), a depth rise under 1 in counts as zero
(sensor noise), and a missing day makes the value null.

### Hourly refresh

User crontab, next to the ski and backup lines (the `XDG_RUNTIME_DIR` and
`DOCKER_HOST` lines at the top of the crontab are needed here too):

```
25 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/snowfall/.refresh.lock make snowfall-refresh >> /data/snowfall/refresh.log 2>&1
```

`/data/snowfall/` must exist (`mkdir -p /data/snowfall`). A run takes a few
seconds and logs about three lines (`unchanged`, `published <revision> (max
N in)`, or an error). It exits 1 if anything failed. The log grows by
about 10 MB a year; truncate it whenever.

`make snowfall-refresh ARGS=--dry-run` downloads and validates but writes
nothing. `ARGS="--region colorado"` limits it to one region.

### Validation and failures

A file is refused (and whatever was served stays) unless it is one band,
EPSG:4326, 0.04° pixels, covers the region, is at least half real data in
the region window, and has values from 0 to 200 in. A refused newer issue
falls back to the served one; NOAA being down keeps the served one.

`GET /status` has a `snowfall` item that turns unhealthy when a period's
newest issue is more than 36 h old (cron stopped, or NOAA stopped
publishing) or missing. `/readyz` does not depend on it.

### Storage and recovery

Revisions not referenced by `latest.json` are deleted after 7 days. Nothing
here needs backing up: `scripts/backup.sh` only copies `dem-cogs` with
`BACKUP_INCLUDE_COGS=true`, and a lost prefix is rebuilt by the next run.

To start over, delete `dem-cogs/snowfall/<region>/latest.json` (MinIO
console) and run `make snowfall-refresh`: it picks the newest issue from the
last 3 days.

## 9. Weather layers

Snow depth, current temperature, wind now and radar, all from NOAA, built the
same way as recent snowfall (§8). A cron job fetches the NOAA grid, validates it, clips
it to each region's `max_bounds` and writes a COG plus `latest.json` to
`dem-cogs/<layer>/<region>/` in MinIO. The API serves `meta` and
TiTiler-rendered tiles under `/tiles`, so tile tokens, rate limits and the
Cloudflare rule apply. The shared code is `app/services/rasterlayer.py`; the
routes are in `app/routers/weather_tiles.py`. Wind is the one exception to
"COG plus tiles": it is a GeoJSON file of arrows (see Wind now).

Downloads send `WEATHER_USER_AGENT` (`.env.example`). These layers are
online-only and never go into offline packs.

### Sources and credits

All of it is US government data: free for any lawful use, including in an app
with in-app purchases. Show the credit whenever the layer is on (`meta` gives
`attribution` and `attribution_url`), don't use NOAA/NWS logos, and don't
imply that NOAA endorses the app or that recoloured data is official NOAA
output (<https://www.weather.gov/disclaimer>).

| Layer | Source | Credit | Terms |
| --- | --- | --- | --- |
| Snow depth | NOHRSC SNODAS, daily, 1 km, distributed by NSIDC as G02158 | NOAA/NWS NOHRSC SNODAS | NSIDC asks for a citation as a condition of use: <https://nsidc.org/data/g02158/versions/1>. The app's credits page should carry: National Operational Hydrologic Remote Sensing Center. 2004. Snow Data Assimilation System (SNODAS) Data Products at NSIDC, Version 1. G02158. NSIDC. doi:10.7265/N5TB14TC |
| Current temperature | RTMA 2.5 km hourly analysis, 2 m temperature, from NOAA's AWS bucket (NOMADS as fallback) | NOAA/NWS RTMA | <https://registry.opendata.aws/noaa-rtma/> (NOAA Open Data Dissemination: use as desired, attribution requested) |
| Wind now | RTMA 2.5 km hourly analysis, 10 m wind direction, speed and gust, from the same file as current temperature | NOAA/NWS RTMA | As current temperature |
| Radar | MRMS merged base reflectivity (QC), 0.01°, every 2 min, from NCEP (NOAA's AWS bucket as fallback) | NOAA/NWS MRMS | <https://registry.opendata.aws/noaa-mrms-pds/> (NOAA Open Data Dissemination: use as desired, attribution requested) |
| Spot forecast | api.weather.gov (`/points`, `/gridpoints`, `/forecast`) | NOAA/National Weather Service | <https://www.weather.gov/documentation/services-web-api> ("open data, free to use for any purpose"; a User-Agent is required) |

### Cron

User crontab, next to the snowfall line (the `XDG_RUNTIME_DIR` and
`DOCKER_HOST` lines at the top of the crontab are needed here too). Each
job needs its directory (`mkdir -p /data/snowdepth /data/temperature /data/radar`).

```
35 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/snowdepth/.refresh.lock make snowdepth-refresh >> /data/snowdepth/refresh.log 2>&1
50 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/temperature/.refresh.lock make temperature-refresh >> /data/temperature/refresh.log 2>&1
3-59/5 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/radar/.refresh.lock make radar-refresh >> /data/radar/refresh.log 2>&1
```

Every job takes `ARGS=--dry-run` (download and validate, write nothing) and
`ARGS="--region colorado"`, and exits 1 if anything failed.

### Snow depth

```
https://noaadata.apps.nsidc.org/NOAA/G02158/masked/YYYY/MM_Mon/SNODAS_YYYYMMDD.tar
  -> make snowdepth-refresh (hourly at :35, app/jobs/snowdepth_refresh.py)
  -> MinIO dem-cogs/snowdepth/<region>/<YYYYMMDD>-<sha8>.tif
     MinIO dem-cogs/snowdepth/<region>/latest.json
  -> GET /tiles/snowdepth/meta?region=colorado
     GET /tiles/snowdepth/{z}/{x}/{y}?region=colorado&v=<revision>
```

- One analysis a day, valid at 06Z (midnight MDT); NSIDC posts the archive
  around midday. The run asks for days newer than the served one and
  re-checks the served day with `If-None-Match`, so most runs are one
  304 or 404.
- The archive is a ~8 MB tar of gzipped flat binaries. Only product 1036
  (snow depth) is read: big-endian int16 millimetres, nodata -9999, 1/120°
  grid whose extent comes from the `.txt` header. Served in **inches**.
- Refused unless the header says int16 millimetres at 1/120°, the grid size
  matches, the archive's date is the day asked for, at least half the
  region has data, and values are 0–1000 in.
- About 350 KB per region per day. `meta` sets `stale` after 48 h.

Legend, inches (under 0.5 in is transparent; one list in
`app/services/snowdepth.py`):

| 0.5–6 | 6–12 | 12–24 | 24–36 | 36–48 | 48–72 | 72–96 | 96–120 | 120+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `#dcebf7` | `#b3d3ee` | `#84b6e1` | `#5596d1` | `#3474bc` | `#2a52a0` | `#40388c` | `#5f2783` | `#7d1670` |

### Current temperature

```
https://noaa-rtma-pds.s3.amazonaws.com/rtma2p5.YYYYMMDD/rtma2p5.tHHz.2dvaranl_ndfd.grb2_wexp(.idx)
  (fallback https://nomads.ncep.noaa.gov/pub/data/nccf/com/rtma/prod/...)
  -> make temperature-refresh (hourly at :50, app/jobs/temperature_refresh.py)
  -> MinIO dem-cogs/temperature/<region>/<YYYYMMDDHH>-<sha8>.tif
     MinIO dem-cogs/temperature/<region>/latest.json
  -> GET /tiles/temperature/meta?region=colorado
     GET /tiles/temperature/{z}/{x}/{y}?region=colorado&v=<revision>
```

- RTMA is an hourly analysis (what the temperature is, not a forecast) on
  the 2.5 km NDFD Lambert grid. Hour H appears about 40 min after H, hence
  :50. An analysis file is ~100 MB; the job reads its `.idx` and downloads
  only the `TMP:2 m above ground` message (~6 MB) with a Range request, so
  about 150 MB a day.
- AWS is tried first, then NOMADS. One unreachable source isn't a failure
  if the other has the hour; a refused file is. Hours newer than the served
  one are tried newest first (6 h back); analyses aren't rewritten, so a
  run with nothing new makes no requests at all.
- Reprojected (bilinear) to a 0.025° lon/lat grid over the region and served
  in **°F**. `meta` adds `min_f`, `max_f` and `source`, and sets `stale`
  after 3 h.
- Refused unless it is one `TMP` band in `[C]` for the hour asked for, on a
  projected 2.5 km grid, covers at least half the region, and is -60 to
  60 °C.
- In the mountains the analysis can be several degrees off in valley
  inversions; the app should label it as an analysis.
- The same run also publishes wind now (below) from the same file. Each
  layer has its own `latest.json` and is published on its own: a refused
  wind file doesn't hold back temperature, and an hour one layer missed is
  filled in later without fetching the other again.

Legend, °F (one list in `app/services/temperature.py`, open at both ends):

| < -10 | -10–0 | 0–10 | 10–20 | 20–32 | 32–40 | 40–50 | 50–60 | 60–70 | 70–80 | 80+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `#4a2a7a` | `#6c4fb0` | `#4560c8` | `#3f8fd2` | `#7fc6e8` | `#b7e2b1` | `#e6f2a2` | `#fee08b` | `#fdae61` | `#f46d43` | `#c2304a` |

### Wind now

```
same RTMA file as current temperature
  -> make temperature-refresh (the same hourly run at :50)
  -> MinIO dem-cogs/wind/<region>/<YYYYMMDDHH>-<sha8>.geojson.gz
     MinIO dem-cogs/wind/<region>/latest.json
  -> GET /tiles/wind/meta?region=colorado
     GET /tiles/wind/<revision>.geojson?region=colorado
```

- The `WDIR`, `WIND` and `GUST` messages at 10 m sit next to each other in
  the file, so they are one extra Range request (~17 MB) next to the
  temperature (~6 MB): about 550 MB a day for both.
- Arrows, not tiles: one gzipped GeoJSON FeatureCollection of points per
  region and hour on a 0.05° grid (Colorado: 16,000 points, ~120 KB
  gzipped). One request per hour shown, whatever the zoom, so it barely
  touches the tile rate limits; vector tiles would cost a dozen per view per
  hour.
- Each point has `dir` (degrees true the wind blows **from**; `null` in calm
  air), `mph`, `gust_mph` (`null` where RTMA has none) and `lvl`: 0 on
  every 4th row and column (0.2°), 1 on every 2nd (0.1°), else 2. `meta`'s
  `levels` give the zoom band for each `max_lvl`; the app shows
  `lvl <= max_lvl`, so arrows thin out when zoomed out and stay put when
  zooming in.
- Direction is never interpolated (350° and 10° would average to 180°): the
  job turns direction and speed into east/north components on RTMA's grid,
  reprojects those (bilinear) to 0.025°, and averages 2×2 cells per arrow.
  Speed is the mean speed, gust the highest gust in the block.
- Refused unless the three messages are `WDIR` in `[deg true]` and `WIND`
  and `GUST` in `[m/s]` for the hour asked for, on the same projected
  2.5 km grid, cover at least half the region, directions are 0–360° and
  speeds 0–200 mph.
- `meta` has `units` (`mph`), `direction` (`from`), `attribution`,
  `attribution_url`, `bounds`, `step_deg`, `levels` and `available`; with
  data also `valid`, `revision`, `fetched`, `source`, `max_mph`,
  `max_gust_mph`, `points`, `stale` (after 3 h) and `hours`: a list of
  `time`, `kind` (`analysis`) and `url`. Forecast hours (NBM, docs/PLAN.md
  "Later") would join that list.
- `<revision>.geojson` is served with `Content-Encoding: gzip` and cached
  for a day (the revision is in the path). Any revision still in MinIO is
  served, so an app holding a meta from before the last run still works;
  404 (cached 60 s) once it has been cleaned up after 7 days, or if the
  revision isn't `YYYYMMDDHH-<8 hex>`.
- 10 m wind in a 2.5 km analysis is smoothed over ridges and gullies: it
  shows the flow the terrain sits in, not what a ridgeline feels.

### Radar

```
https://mrms.ncep.noaa.gov/2D/MergedBaseReflectivityQC/   (listing, ~1 day of files)
  (fallback https://noaa-mrms-pds.s3.amazonaws.com/CONUS/MergedBaseReflectivityQC_00.50/YYYYMMDD/)
  -> make radar-refresh (every 5 min at :03, :08, ..., app/jobs/radar_refresh.py)
  -> MinIO dem-cogs/radar/<region>/<YYYYMMDDHHMM>-<sha8>.tif   one per frame
     MinIO dem-cogs/radar/<region>/latest.json                 frames, oldest first
  -> GET /tiles/radar/meta?region=colorado
     GET /tiles/radar/{YYYYMMDDHHMM}/{z}/{x}/{y}?region=colorado
```

- MRMS writes a CONUS mosaic about every 2 min (~1.3 MB gzipped GRIB2).
  The layer keeps **one frame per 10 minutes for the last 2 hours** (12–13
  frames). A frame is filled once its mark is 3 min old, from the newest file
  in the 10 minutes up to the mark (`observed` is that file's time), and is
  never rewritten. Each run fills every missing frame in the window, so a
  short outage is backfilled. A run with nothing to fill makes no requests.
- NCEP is listed first; AWS only when NCEP lacks a frame or is down. One
  source down isn't a failure while the other fills the frames; a refused
  file is.
- Values are dBZ. MRMS writes -99 for "covered, no echo" and -999 for
  "outside radar coverage". Both, and anything under 5 dBZ, are stored as
  nodata, so a clear-sky frame is a ~7 KB COG. `max_dbz` is `null` when
  nothing was measured.
- Refused unless it is one band in `[dBZ]` with the file's own valid time,
  a 0.01° lon/lat grid that covers the region, at least half the region
  inside radar coverage, and no value over 100 dBZ.
- Downloads: about 1.3 MB × 144 frames ≈ 190 MB a day, plus a 176 KB
  listing per run that has a frame to fill.

`meta` returns `units` (`dBZ`), `attribution`, `bounds`, `maxzoom` (9),
`legend`, `interval_min` (10), `available`, `frames` (each `time`,
`observed`, `max_dbz` and `tiles`; add `t`), and when there are frames
`latest` and `stale` (newest `observed` over 20 min old). Frames older than
2 h are not listed. If MRMS is down for 2 h, `frames` is empty and the app
should say radar is unavailable rather than show old radar.

| Endpoint | Cache-Control |
| --- | --- |
| `GET /tiles/radar/meta?region=` | 60 s |
| `GET /tiles/radar/{frame}/{z}/{x}/{y}?region=` (a frame never changes) | 1 day |
| frame not in `latest.json` (expired or never filled) | 404, 60 s |
| MinIO unreadable | 503 `no-store` |

Frame files are deleted 3 h after they were written. Scrubbing all frames
with ~20 tiles in view is ~260 tile requests, within the anonymous tile
limit (600/min).

Legend, dBZ (one list in `app/services/radar.py`; under 5 is transparent):

| 5–10 | 10–15 | 15–20 | 20–25 | 25–30 | 30–35 | 35–40 | 40–45 | 45–50 | 50–55 | 55–60 | 60–65 | 65+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `#04e9e7` | `#019ff4` | `#0300f4` | `#02fd02` | `#01c501` | `#008e00` | `#fdf802` | `#e5bc00` | `#fd9500` | `#fd0000` | `#d40000` | `#bc0000` | `#f800fd` |

### `meta` and tiles (snow depth, temperature; wind is above)

`GET /tiles/<layer>/meta?region=` returns `units`, `attribution`,
`attribution_url`, `bounds`, `maxzoom` (overzoom past it), `legend` (`min`,
`max`, `color`, `label`; `null` is an open end) and `available`. When data
exists it also has `valid` (UTC), `revision`, `fetched`, `stale` and `tiles`
(a URL template that already carries `v`; add `t`), plus the layer's stats.

| Endpoint | Cache-Control |
| --- | --- |
| `meta` | 300 s; 60 s before the first ingest |
| tile with the current `v` | 1 day |
| tile without `v` or with an old one (serves the current data) | 15 min |
| no data yet, or MinIO unreadable | 503 `no-store` |

### Spot forecast

`GET /weather/forecast?lat=&lon=&t=<tile token>` (`app/routers/weather.py`,
`app/services/nws.py`). Not under `/tiles` and not cached by Cloudflare.

- **Access:** a valid tile token of either tier (signed-out installs have
  one; tokenless is never allowed, whatever `TILES_ALLOW_TOKENLESS` says),
  then the per-IP `weather` bucket, 60/min and 600/h. 401 without a token,
  429 with `Retry-After` over the limit. A public exception, listed in
  CLAUDE.md.
- **NWS calls:** `/points/{lat},{lon}` maps the tap to a 2.5 km grid cell
  (cached 7 days, taps rounded to 3 decimals, ~100 m; "not covered" is
  cached too). `/gridpoints/{office}/{x},{y}` (hourly values) and its
  `/forecast` (text) are fetched together and cached per grid cell for
  45 min, so every tap in a cell shares one fetch. Retries and a circuit
  breaker (`nws`) via `call_with_resilience`. NWS's own responses say
  `max-age=3600`.
- **Response:** `hours` (48 entries from the current UTC hour: `temp_f`,
  `wind_mph`, `gust_mph`, `wind_dir_deg`, `pop_pct`; `null` where NWS has no
  value), `snow` (NWS's own blocks, usually 6 h, as `start`, `end`,
  `snow_in`, not split into hours), `periods` (the next four text periods:
  `name`, `start`, `end`, `short`, `detailed`), `grid` (`office`, `x`, `y`,
  `elevation_ft`: the one elevation NWS forecasts for in that cell),
  `point_elevation_ft` (the tapped point from our DEM; `null` outside our
  regions or if the DEM is slow), `updated` (NWS), `fetched`, `stale`,
  `units`, `attribution`. `Cache-Control: private, max-age=300`.
- **Failures:** a point NWS doesn't cover is 404. If NWS fails, a cached
  forecast up to 12 h old is served with `stale: true` and its `fetched`
  time; otherwise 502 `no-store`. If only the text forecast fails (NWS's
  `/forecast` often 500s on its own), `periods` is `null` and the hours are
  still served. Unexpected units from NWS are a 502, not wrong numbers.

### Failures and recovery

A refused file never replaces what is served, and a NOAA or NSIDC outage
keeps the last good data; `meta` shows its age through `valid` and `stale`.
`GET /status` has an item per layer (`snowdepth`, `temperature`, `wind`, `radar`) that turns unhealthy
past the same limit as `stale`. `/readyz` does not depend on them.

Revisions not referenced by `latest.json` are deleted after 7 days. Nothing
needs backing up; to start over, delete `dem-cogs/<layer>/<region>/latest.json`
and run the job.

## 10. Mountain cameras

Webcam pins from two free sources, merged per region. The backend keeps the
lists; the images are never cached or rehosted by us. Code:
`app/services/cameras.py`, `app/jobs/cameras_refresh.py`,
`app/routers/cameras.py`. CDOT cameras are tabled (docs/PLAN.md, Later).

### Sources, keys and terms

| Source | Key (`.env`) | What we keep | Image | Credit |
| --- | --- | --- | --- | --- |
| NPS API webcams, parks in `cameras.NPS_PARKS` (Colorado: `romo`) | `NPS_API_KEY`, free from <https://www.nps.gov/subjects/developer/get-started.htm>, 1,000 requests/h | id, title, position, page, image URL, image time | live JPEG on nps.gov, loaded by the app directly | "NPS / Rocky Mountain National Park"; no Arrowhead logo |
| Windy Webcams API v3, free tier | `WINDY_WEBCAMS_API_KEY`, free from <https://api.windy.com/keys> (needs a Windy account) | id, title, position, windy.com page, `lastUpdatedOn`; **no image URL** | fetched fresh per tap through `/cameras/{id}/image` (URLs expire after 10 min), loaded from Windy | each image links to its windy.com page, plus the line "Webcams provided by windy.com — add a webcam" linking windy.com and windy.com/webcams/add |

A source with an empty key is skipped by the job, left out by the API and
shown as "not configured" in `/status`. Both keys are sent as headers only
(`X-Api-Key`, `x-windy-api-key`), never in a URL; errors are logged as the
exception type and message, never with request headers.

Terms that shape the code (read Oct 16, 2026):

- **Windy** (<https://api.windy.com/webcams/terms>, full terms at
  <https://account.windy.com/agreements/windy-webcams-api-terms-of-use>):
  the free tier may not be "solely in the paid part of the User
  Application", so the layer is never behind a paid tier (CLAUDE.md). "Use
  images only with URLs provided by the API", no redistribution and no
  publishing of the images, so nothing is cached or proxied. "Link every
  image with either our webcam page or timelapse player", plus the courtesy
  line. The key must not be published, so every Windy call is server-side.
  Windy forbids "continuous scanning of a significant number of available
  Webcams" and "intensive usage", hence one list query a day and the global
  `windy_upstream` cap on image lookups.
- **NPS** (<https://www.nps.gov/aboutus/disclaimer.htm>): NPS material is
  generally public domain, credit "is appreciated", and the Arrowhead may not
  be used without permission. Nothing on caching or hotlinking.

### Storage and refresh

```
NPS API /webcams + each camera's nps.gov page   (every 6 h)
Windy /webcams?bbox=<region max_bounds>         (every 24 h, 50 per call, offset ≤ 1000)
  -> make cameras-refresh (hourly at :10, app/jobs/cameras_refresh.py)
  -> MinIO dem-cogs/cameras/<region>/nps.json
     MinIO dem-cogs/cameras/<region>/windy.json
```

Each run asks only the sources that are due (their file's `fetched` is
older than the interval, less 10 min), so most runs make no requests.
`ARGS=--force` asks every source now; `ARGS=--dry-run` fetches and checks
but writes nothing; `ARGS="--source windy"` and `--region` narrow it. Exits
1 if any source failed.

Cron (user crontab, with the `XDG_RUNTIME_DIR` and `DOCKER_HOST` lines from
§9; `mkdir -p /data/cameras` first):

```
10 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/cameras/.refresh.lock make cameras-refresh >> /data/cameras/refresh.log 2>&1
```

- **NPS:** the API lists the cameras but has no live image and often a
  park-centre placeholder position (several cameras share one point). The
  job reads each camera's page (`<img id="webcamRefreshImage">`) for the
  image and keeps cameras with a position of their own or a hand-set one in
  `NPS_POSITIONS`. Cameras with neither, an "inactive" image or a status
  other than Active are left out. Longs Peak ("seen from the Tahosa
  Valley") has no hand-set position yet; add one to `NPS_POSITIONS` to show
  it. The two hand-set ones are approximate.
- **Windy:** every active webcam in the region's `max_bounds`. The free
  tier stops at offset 1000 (1,050 webcams); the job logs a warning if the
  region has more.
- **Elevation:** `elevation_m` from our DEM at the camera's position
  (`cog_sampler.sample_elevations`), kept from the last list for cameras
  that haven't moved. `null` outside the DEM or if it can't be read.

### API

Both need a tile token (either tier; tokenless is never allowed) and count
against the per-IP `cameras` bucket, 60/min and 600/h. 401 without a token,
429 with `Retry-After` over the limit. A public exception, listed in
CLAUDE.md. Not under `/tiles`, so Cloudflare doesn't cache them.

- `GET /cameras?region=colorado&t=` is a GeoJSON FeatureCollection of
  points. Properties: `id` (`nps:<id>`, `windy:<id>`), `name`, `source`,
  `elevation_m`, `credit`, `link` (the camera's page; Windy requires every
  image to link to it), `updated` (when the source last updated the image,
  as of the last list), `image_url` (set for NPS: load it directly; `null`
  for Windy) and `image` (the path below). `sources` gives per source
  `state` (`ok`, `stale`, `missing`, `expired`, `unavailable`,
  `not_configured`), `fetched` and `attribution` (text and links to show
  while the layer is on). `Cache-Control: private, max-age=300`.
- `GET /cameras/{id}/image?region=colorado&t=` gives `url`, `taken`,
  `expires`, `credit`, `link`. Windy: a fresh URL from Windy, kept per
  worker for 5 min; `expires` is 10 min after it was fetched. Each Windy
  call also counts against the global `windy_upstream` bucket (60/min,
  1000/h, all users together), 429 over it. NPS: the stored URL, with
  `taken` from a HEAD's `Last-Modified` (kept 60 s; the stored time if
  nps.gov doesn't answer), `expires: null`. 404 for an id that isn't in
  the stored list (so this can't fetch arbitrary cameras), 502 `no-store`
  if Windy can't give an image. `Cache-Control: private, max-age=60`.

### Failures and recovery

- **One source down:** each source is fetched and checked on its own. A
  list is written only if it has cameras and at least half as many as the
  list it replaces; otherwise the old one stays served. The other source is
  unaffected.
- **Old lists:** cameras don't move, so a list is served until 7 days old
  (`state: stale` after 3 intervals: NPS 18 h, Windy 72 h), then left out
  (`expired`). If MinIO can't be read for one source, that source is
  `unavailable` and the rest is served; if it fails for all, 503 `no-store`.
- **`/status`:** one `cameras` item, unhealthy when a configured source's
  list is missing or older than 3 intervals. Unconfigured sources are only
  noted.
- **To start over:** delete `dem-cogs/cameras/<region>/<source>.json` and
  run `make cameras-refresh ARGS=--force`. Nothing needs backing up.

## 11. Road conditions

CDOT's road conditions, incidents and planned events from the keyed COtrip
data feeds, for the drive to the trailhead. The backend keeps the latest of
each feed; the app reads one endpoint. Code: `app/services/roads.py`,
`app/jobs/roads_refresh.py`, `app/jobs/roads_sample.py`,
`app/routers/roads.py`, `app/log_redact.py`.

### Source, key and terms

- **Feeds:** `https://data.cotrip.org/api/v1/<feed>?apiKey=` (user guide
  v1.6). We use `roadConditions` (198 segments, one LineString each, with
  `currentConditions[]`), `incidents` and `plannedEvents` (MultiPoint start
  and end, `type`, `category`, `laneImpacts`, `additionalImpacts`, traveler
  message, schedule). Not used: `weatherStations`, `signs`, `snowPlows`,
  `destinations`, `cwz`, `wzdx`. No camera feed (CDOT cameras are tabled,
  PLAN.md Later).
- **Key:** `COTRIP_API_KEY` in `.env`, from <https://manage-api.cotrip.org>
  (accept the terms; a CDOT admin approves it). Generating a new key there
  invalidates the old one. Empty turns the whole layer off: the job fetches
  nothing, `/roads` answers `not_configured`, `/status` notes it. Use that
  if CDOT revokes access.
- **The key is in the URL.** COtrip accepts it only as `?apiKey=`, and
  httpx puts URLs in its INFO lines and its error messages. So the jobs set
  the `httpx` logger to WARNING, turn every request error into "HTTP 500" or
  the exception type (`roads_refresh.describe`), never log a traceback, and
  `app/log_redact.py` redacts `apiKey=` and the key itself from every log
  record as a backstop (tested in `tests/test_roads.py`). Never paste a
  COtrip URL from a debugging session into an issue or chat.
- **Terms** (portal terms of service, read Oct 16, 2026): applications
  "must include the following disclaimers on their sites" — the full text
  is `roads.DISCLAIMER`, served as `disclaimer` by `/roads`, and the app
  shows it in the layer info and on the credits page. CDOT may "modify
  and/or discontinue providing any or all of the data feeds at any time and
  ... require the termination of any and all displaying"; the format may
  change. The terms say nothing on caching, polling rate or redistribution.
- **Paging quirks:** pages come 100 at a time with the next cursor in the
  `Next-Offset` header (`None` on the last page). `offset=0` answers 500,
  so the first page is sent without one, and `roadConditions` wraps the
  cursor in a Python bytes repr (`b'...'`), which the job unwraps.

### Categories

`category` is one of `closed`, `closed_one_direction`, `chain_law`, `traction_law`,
`avalanche_control`, `crash`, `construction`, `dry`, `no_data`, `other`.
A value maps to a category only if the real feed has shown it; everything
else is `other` and keeps CDOT's text, so the app shows exactly what CDOT
says. Chain law, traction law, avalanche control and crash have no mapping
yet: nothing in the feed on Oct 18, 2026 (a dry day) used them. Never add a
mapping from a guess at CDOT's wording; add it from the sample below.

| Feed | CDOT value | Category |
| --- | --- | --- |
| roadConditions `conditionDescription` | `3 - dry` | `dry` |
| | `seasonal closure` | `closed` |
| | `no data` | `no_data` (Trail Ridge, Cottonwood, Kebler and Guanella show this in October, not "closed") |
| | `forecast text included` (source NDFD) | not a condition: its `additionalData` is the segment's `forecast` |
| | anything else | `other` |
| incidents, plannedEvents | every direction in `laneImpacts` has all its lanes closed | `closed` |
| | some directions, not all, have all their lanes closed | `closed_one_direction` (which: `closed_directions`) |
| | `category` `Construction` | `construction` |
| | anything else (`Maintenance`, `ITS Work`, `Utility`, `Traffic`) | `other` |

- **Lanes:** `laneClosures` is a 16-bit mask as hex, most significant bit
  first: bit 0 the left shoulder, bits 1 to `laneCount` the lanes from the
  median out, bit 15 the right shoulder. "Full closure" is every lane bit
  set, the guide's appendix table A-3 (`4000` or `C001` for one lane,
  `7000`/`F001` for three). On Oct 18 the decoding matched `closedLaneTypes`
  on every record, and the two events it marks closed both say "Road closed
  due to ..." in CDOT's message. A direction with no lane count is never
  closed.
- **One direction closed** (I-70 Floyd Hill westbound overnight, or
  alternating traffic on a two-lane road) is `closed_one_direction`, with
  the direction(s) in `closed_directions` ("west"); the app shows "Closed
  westbound". It wins over `construction`. On Oct 19: 4 planned events and
  1 incident.
- A segment shows its most important condition: `closed`, then
  `avalanche_control`, `chain_law`, `traction_law`, `other`, `dry`,
  `no_data`. An unknown condition ranks above `dry`, so it is never hidden.

### Recording CDOT's values (for a week)

`make roads-sample` reads all three feeds (conditions without geometry)
and adds every distinct `conditionDescription`, incident `type`, planned
`type` and both feeds' `additionalImpacts` value to
`dem-cogs/roads/colorado/sample.json`, with `count` (records, summed over
runs), `runs`, `first_seen`, `last_seen`, the category it got and the last
example (segment name or traveler message). `make roads-sample
ARGS=--report` prints it, most common first, and fetches nothing. Run it
every 15 min for a week that includes a storm, then remove the cron line,
read the report and add the chain law, traction law, avalanche control and
crash values to `roads.CONDITIONS` / `roads.EVENT_TYPES`, with a test each.

```
4-59/15 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/roads/.sample.lock make roads-sample >> /data/roads/sample.log 2>&1
```

### Storage and refresh

```
roadConditions?skipGeometry=true   every run      -> dem-cogs/roads/<region>/conditions.json
incidents                          every run      -> dem-cogs/roads/<region>/incidents.json
plannedEvents                      every 30 min   -> dem-cogs/roads/<region>/planned.json
roadConditions (with lines)        daily, or when a new segment id appears
                                   -> dem-cogs/roads/<region>/segments/<YYYYMMDD>-<sha8>.geojson.gz
                                      dem-cogs/roads/<region>/segments.json (revision, ids)
  make roads-refresh (every 5 min, app/jobs/roads_refresh.py)
```

Colorado only (`roads.REGIONS`). Segment lines are simplified to about
20 m (7.4 MB raw to ~140 KB gzipped); a new revision is written only when
the lines change, and old revisions go after 7 days. Planned events and
incidents that have ended are dropped. `unmapped` in each file counts the
values that came out `other`, and each run logs them. `ARGS=--force` asks
every feed now; `ARGS=--dry-run` fetches and checks but writes nothing;
`ARGS="--feed incidents"` narrows it. Exits 1 if any feed failed.

Cron (user crontab, with the `XDG_RUNTIME_DIR` and `DOCKER_HOST` lines from
§9; `mkdir -p /data/roads` first). `2-59/5` keeps it off radar's minutes:

```
2-59/5 * * * * cd /home/ronkerflonk/whumpf && flock -n /data/roads/.refresh.lock make roads-refresh >> /data/roads/refresh.log 2>&1
```

### API

- `GET /roads?region=colorado&t=` needs a tile token (either tier;
  tokenless is never allowed) and counts against the per-IP `roads`
  bucket, 60/min and 600/h (401 without a token, 429 with `Retry-After`).
  A public exception, listed in CLAUDE.md: it only serves the job's files,
  never calls CDOT. `Cache-Control: private, max-age=60`. About 350 KB
  (35 KB gzipped) on Oct 19, with 199 of 434 planned events. The body:
  - `disclaimer` (show it with the data), `attribution`, `categories`.
  - `sources`: per feed `state` (`ok`, `stale`, `missing`, `expired`,
    `unavailable`, `not_configured`, `not_covered`) and `fetched`.
  - `segments`: `url` of the current lines file (below), `revision`,
    `fetched`; `null` until the job has written one.
  - `conditions`: segment id to `name` (CDOT's short name, e.g. "Vail
    Pass"), `description`, `route`, `category`, `conditions` (each with
    `category`, CDOT's `text`, `source` OPERATOR/MDSS, `note`, `start`,
    `end`, `updated`) and `forecast` (NDFD `text`, `start`, `end`).
  - `features`: incidents not yet cleared, and planned events in effect
    now or with a schedule window starting within 48 h
    (`roads.PLANNED_AHEAD`; the stored file keeps all of them), as GeoJSON
    (MultiPoint), id `<feed>:<CDOT id>`. Properties: `feed`, `category`,
    `type` and `cdot_category` (CDOT's), `name`, `route`, `direction`,
    `severity`, `message` (CDOT's traveler message), `impacts`
    (`additionalImpacts`), `lanes` (per direction `lane_count`,
    `lanes_closed`, `all_closed`, `closed_types`), `closed_directions`,
    `start`, `end`, `updated`, `schedule` and `active` (in effect now;
    `null` if a window repeats by weekday, which the feed hasn't used).
- `GET /tiles/roads/segments/<revision>.geojson?region=colorado&t=`: the
  lines, gzipped GeoJSON, one LineString per segment with `id`, `name`,
  `description`, `route`. Under `/tiles`, so the tile token rules apply and
  Cloudflare caches it; `public, max-age=86400`, 404 once cleaned up.

### Failures and recovery

- **One feed down:** each feed is fetched and checked on its own. A feed is
  written only if every page answered; conditions also need at least half
  the segments of the file they replace, planned events at least one event
  (zero incidents is normal). Otherwise the old file stays served.
- **Old data:** conditions and incidents are `stale` after 30 min, planned
  events after 2 h. Past 24 h a feed is `expired` and left out, because an
  old "dry" or a missing closure is worse than nothing. If MinIO can't be
  read for one feed it is `unavailable`; for all three, 503 `no-store`.
- **`/status`:** one `roads` item, unhealthy when a feed is stale or the
  lines are over 3 days old. "not configured" (healthy) with no key.
- **CDOT changes the format:** the checks reject a body that isn't a
  FeatureCollection; new condition texts or types show as `other` with
  CDOT's text and are counted in `unmapped`.
- **To start over:** delete `dem-cogs/roads/<region>/` and run
  `make roads-refresh ARGS=--force`. Nothing needs backing up.

## 12. Offline packs: tile cache and pack worker

The iPhone app downloads a pack as one PMTiles file per layer for a box up
to 25 x 25 km. The `pack-worker` service builds queued packs into Cloudflare
R2, reading every tile through the tile cache below. Code:
`app/services/pack_cache.py` (the cache), `app/services/terrain_tiles.py`
(shared with the live `/tiles` endpoints), `app/services/packs.py` (tiles,
PMTiles, R2) and `app/jobs/pack_worker.py` (the service and CLI). The
endpoints that queue and hand out packs are step B3 (not built yet).

### Layers

| Layer | Zooms | Source | Stored as | Kept until |
|---|---|---|---|---|
| topo | 8-16 | USGS `USGSTopo` | JPEG as received | forever; a 404 for 30 days |
| imagery | 8-16 | USGS `USGSImageryOnly` (NAIP) | JPEG as received | forever; a 404 for 30 days |
| slope | 8-14 | TiTiler, `slope.tif` (no hires, as the app) | 512 px palette PNG | `slope.tif` changes |
| aspect | 8-14 | TiTiler, `aspect.tif` | 512 px palette PNG | `aspect.tif` changes |
| elevation | 8-14 | DEM render (z13+ hires, 10 m fallback) | 256 px Terrarium PNG, level 9 | `dem.tif` (or `dem_hires.tif`) changes |
| ski | 8-14 | `/data/martin-tiles/ski_<region>.mbtiles` | gzipped MVT as stored | the file is republished |

Slope, aspect and elevation use the live endpoints' own code and TiTiler
parameters, so a pack tile decodes to the same pixels or metres as the live
tile (tests in `tests/test_pack_cache.py`). Elevation with no data is the
live endpoint's flat 0 m tile. A version is the MinIO ETag of the COGs
(looked up at most every 10 min) or the ski file's mtime; a tile of an old
version is made again when next asked for.

### Files and settings

- `PACK_CACHE_DIR` (default `/data/pack-cache`): one SQLite file per layer,
  `topo.sqlite`, `imagery.sqlite`, and per region for ours,
  `slope_colorado.sqlite` and so on. Table `tiles(z, x, y, data, made_at,
  source_version, last_used_at)`; an empty `data` is a real empty answer
  (USGS or TiTiler 404, no ski runs). Written only by the pack worker. The
  api mounts the directory read-only for `/status`; create it before the
  first `make up` (`mkdir -p /data/pack-cache`, done on the VM Oct 19, 2026).
- `PACK_CACHE_MAX_GB` (default 100): above it the least recently used tiles,
  across all layers, are deleted down to 90% and the files shrink. Checked
  after every 256 MB written. A pack is ~270 MB (topo ~60, imagery ~95,
  slope ~40, aspect ~50, elevation ~25), so 100 GB holds ~370 packs that
  don't overlap. `/status` shows `pack_cache: "12.3 GB of 100 GB"` and is
  unhealthy only above 110% (eviction not keeping up).
- `USGS_CONTACT`: an email address for the User-Agent of every USGS request.
  Empty turns topo and imagery off (they refuse to fetch).

### Limits

- USGS: at most 4 requests at a time, retried with backoff on network
  errors, 429 (Retry-After honoured up to 60 s) and 5xx. Failures, HTML
  error pages and empty bodies are never stored.
- Our tiles: at most 2 at a time in their own 2-thread pool, so packs don't
  slow the live map (slope and aspect go through the same TiTiler).
- These limits hold within one process. **The pack worker must run as a
  single process** (see below; a second one exits).

### Clearing

To drop a layer, stop the pack worker (`make stop-pack-worker`) and delete
its `.sqlite` file (with `-wal` and `-shm`). Nothing needs backing up;
everything can be made again.

### The pack worker

- Service `pack-worker` in `compose.yml`: the api image running
  `python -m app.jobs.pack_worker run`. **One process, one build at a
  time; never scale it.** It holds a Postgres advisory lock, so a second
  worker (or `make pack-build-once` while the service runs) exits.
- It never runs migrations. On start it waits for the `packs` table (the
  api's entrypoint creates it), empties `/data/pack-cache/build/`, and puts
  packs left `building` by a crash back in the queue (a pack fails after
  its third start). Then it takes the oldest `queued` pack every 5 s.
- `make stop-pack-worker` (or a deploy) stops a build cleanly: its uploads
  are deleted and it goes back to the queue without using up a start.
- Mounts: `/data/pack-cache` read-write (it is the only writer),
  `/data/martin-tiles` read-only (ski). Stop it with the api before a
  database restore (`make stop-api stop-pack-worker`).

### A build

1. The box is rounded out to the zoom 14 grid (~1.9 km tiles in Colorado).
   Every layer covers that at its zooms (table above); elevation also gets
   two more zoom 14 tiles on every side, for the runout check on the phone.
   Lower zooms are the parents of that area, higher zooms its children.
2. One layer at a time, tiles in PMTiles tile ID order through the cache
   (32 asked ahead; the cache's limits decide how many run at once), into a
   PMTiles v3 file in `/data/pack-cache/build/<pack id>/`: tiles as made
   (JPEG, PNG, gzipped MVT), gzipped directories, identical tiles stored
   once (by SHA-256; `pmtiles`' own Writer used Python's `hash()`), empty
   answers left out, the covered bounds and zooms in the header. Metadata:
   layer, region, pack id, source version, attribution, tile size,
   `encoding: terrarium` for elevation, `vector_layers` for ski. A layer
   with no tiles at all (no ski runs in the box) gets no file.
3. The layer's source version is read before and after; if a COG or the
   ski file changed meanwhile, the layer is built once more, then the pack
   fails.
4. Upload to R2 at `packs/<pack id>/<layer>.pmtiles` with Content-MD5,
   then checked by R2's ETag (the MD5) and size. Bytes and MD5 go in
   `files`.
5. `ready` with `built_at`; or `failed` with a short reason, its uploads
   deleted, and the next pack starts. The build folder is removed either
   way. Nothing logs the R2 keys or endpoint (botocore messages are reduced
   to their error code).

Progress (`tiles_done` of `tiles_total`) is saved about once a second. Each
layer logs a line `pack <id> topo: <n> tiles (<n> made, <n> from the
cache), <n> in the file, <bytes> bytes, <seconds> s`; for topo and imagery
"made" is the number of tiles asked of USGS.

### The packs table (migration `0002_packs`)

`id` (UUID), `region`, `bbox` (the box as asked for, POLYGON 4326),
`layers`, `source_versions` (`{layer: version}`, null for USGS), `status`
(`queued | building | ready | failed`), `attempts`, `tiles_done`,
`tiles_total`, `error`, `files` (`[{layer, version, bytes, md5, key}]`;
bytes 0 and key null for a layer with no tiles), `requested_by` (a user id,
no FK; set to null when that account is deleted), `created_at`, `built_at`,
`last_requested_at`. Reusing a ready pack for the same box and cleaning old
packs out of R2 are B3's job; nothing deletes them yet.

### Settings

`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an R2 API
token with Object Read & Write on the packs bucket only) and
`R2_BUCKET_PACKS`. Passed to the api too, for B3's download links.
`R2_ACCOUNT_ID` is the bare 32-character account ID, not the endpoint URL.
The worker checks these first when it starts: if one is empty or the ID
isn't 32 hex characters it logs why and exits (it restarts, so `make ps`
shows it restarting) before making any tile. Fix `.env`, then `make up`
(a restart keeps the old environment).

### Commands

```bash
make pack-queue ARGS="--bbox=W,S,E,N"     # the = is needed: west is negative
make pack-list                            # newest packs: status, progress, files, errors
make logs-pack-worker
make stop-pack-worker                     # make up starts it again
make pack-build-once                      # only while the service is stopped
```

A stuck or unwanted pack: `make stop-pack-worker`, then in `make psql`
`UPDATE packs SET status = 'failed', error = 'cancelled by hand' WHERE id
= '<id>';` and `make up`.

### First deploy (B2)

`0002_packs` is the first migration since prod was stamped at
`0001_baseline`; do the restore rehearsal first (§6).

1. The R2 bucket named in `R2_BUCKET_PACKS` exists and the four `R2_*`
   are set in `.env`; `USGS_CONTACT` is set; `/data/pack-cache` exists.
2. `make up`: builds the image; the api runs `alembic upgrade head`
   (creates `packs`) and `pack-worker` starts.
3. `make health` is `"ready": true` with schema at `0002_packs`;
   `make ps` lists `whumpf-pack-worker`; `make logs-pack-worker` shows
   `pack worker ready`.

### End-to-end check: Berthoud Pass (after the first deploy)

About 10 x 10 km around the pass. Expected: rounded to 11.3 x 13.1 km, and
topo 915, imagery 915, slope 75, aspect 75, elevation 163, ski 75 tiles.

1. Queue it, with every layer:

   ```bash
   make pack-queue ARGS="--bbox=-105.835,39.753,-105.719,39.843"
   ```

   It prints the pack id, the size before and after rounding and the tile
   count per layer; check them against the numbers above.
2. Watch it build: `make logs-pack-worker`. Note each layer's line (time,
   tiles made vs from the cache, bytes) and the final `ready` line. The
   first run fetches nearly every USGS tile; slope, aspect and elevation
   are made from the COGs.
3. `make pack-list`: status `ready`, `tiles_done` = `tiles_total` (2,218),
   six files (ski may have no file if OSM has no runs in the box) with
   bytes and MD5.
4. Read the files back from R2 and compare MD5s with `make pack-list`:

   ```bash
   set -a; . ./.env; set +a
   mkdir -p /tmp/berthoud
   podman run --rm --network host \
       -e ENDPOINT=https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com \
       -e KEY=$R2_ACCESS_KEY_ID -e SECRET=$R2_SECRET_ACCESS_KEY -e BUCKET=$R2_BUCKET_PACKS \
       -v /tmp/berthoud:/out --entrypoint sh quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z -c \
       'mc alias set r2 "$ENDPOINT" "$KEY" "$SECRET" >/dev/null && \
        mc cp --recursive "r2/$BUCKET/packs/<pack id>/" /out/'
   md5sum /tmp/berthoud/*.pmtiles
   ```

   Optionally open one at <https://pmtiles.io> (drag the file in; it stays
   in the browser) to look at the tiles and header.
5. Queue the same box again. The second build should show `0 made` for
   topo and imagery (all from the cache) and take seconds per layer.
6. Report per layer: time, size, USGS tiles fetched vs from the cache, for
   both runs. Then `rm -r /tmp/berthoud`. The two packs stay in R2 until
   B3's cleanup (or delete `packs/<id>/` by hand).

## Troubleshooting

### `api` container restarts repeatedly

Usually means `postgis` didn't finish its initdb before `api` tried to
connect. Wait 30 seconds; compose v2 will restart `api` automatically once
the healthcheck passes. If it doesn't settle, `make logs-db` and look for
errors during `CREATE EXTENSION`.

### `titiler` returns 500 when loading COGs

Almost always a MinIO connectivity issue. TiTiler talks to MinIO through
rasterio/GDAL's S3 reader. Check that the env vars in `compose.yml` match
what's in `.env`:

```bash
docker compose exec titiler env | grep AWS_
```

`AWS_S3_ENDPOINT=minio:9000` — not `http://minio:9000`; the scheme is set
separately via `AWS_HTTPS=NO`.

### Redis `NOAUTH` / `WRONGPASS` errors

`REDIS_PASSWORD` in `.env` must be set, and the redis container must be
recreated after changing it (`make up`, not `make restart`) so the new
`--requirepass` takes effect. The api reads it through `REDIS_URL`, which
compose builds from the same variable. Check with:

```bash
docker compose exec redis redis-cli ping    # PONG (uses REDISCLI_AUTH)
```

### `martin` can't see new tables

Martin discovers PostGIS tables at startup. After you `CREATE TABLE` or
migrate, run:

```bash
docker compose restart martin
```

Or hit its `/catalog` endpoint and it will return only what's registered.

### `permission denied` on a bind mount

You're not using bind mounts for data (we use named volumes), so this should
not come up. If you added a bind mount: rootless Podman remaps UIDs, so
bind-mounted directories on the host need either the `:Z` SELinux label or
`userns_mode: "keep-id"`. See
<https://www.redhat.com/en/blog/rootless-podman-makes-sense>.

### Out of disk

```bash
docker system df
docker system prune --all --volumes   # nuclear — destroys stopped-container data
```

The VM disk is 100 GB; DEM data lives on the separate `/data` mount.
Current footprint: `dem.tif` (10m, full CO) ~4 GB, `dem_hires.tif` (1m,
partial CO) ~161 GB, derived COGs (slope/aspect/hillshade) ~20 GB,
MBTiles (10m + 1m partial) ~36 GB. Full 1m Colorado would be ~500+ GB.

### Podman version check

```bash
podman version
```

If you hit issues traceable to Podman itself (esp. `depends_on` timing
races), upgrade to a newer Podman from the Kubic project:

```bash
# Example: OpenSUSE Kubic maintained a newer stream; as of early 2026 the
# supported way on Ubuntu 24.04 is to build from source or grab a nightly
# deb. The Ubuntu 24.04-packaged 4.9.3 is usually good enough for Whumpf.
```

## Reference

- [whumpf-stack.md](../whumpf-stack.md) — the "why" behind every tech choice
- [architecture.md](./architecture.md) — architecture diagrams and flow
- [data-sources.md](./data-sources.md) — feed URLs, schemas, licensing
