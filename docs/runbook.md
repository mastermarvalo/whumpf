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
the API's `Cache-Control` (a day for most tiles). Check it with:

```bash
curl -sI "https://api.whumpf.co/tiles/slope/10/210/394?region=colorado" | grep -i cf-cache-status
# first request MISS, then HIT; DYNAMIC means the rule is not applying
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

Restore a local dump (overwrites the live DB; stop the api first so nothing
writes during the restore):

```bash
make stop-api
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

Snow depth, current temperature and radar, all from NOAA, built the same way
as recent snowfall (§8). A cron job fetches the NOAA grid, validates it, clips
it to each region's `max_bounds` and writes a COG plus `latest.json` to
`dem-cogs/<layer>/<region>/` in MinIO. The API serves `meta` and
TiTiler-rendered tiles under `/tiles`, so tile tokens, rate limits and the
Cloudflare rule apply. The shared code is `app/services/rasterlayer.py`; the
routes are in `app/routers/weather_tiles.py`.

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

Legend, °F (one list in `app/services/temperature.py`, open at both ends):

| < -10 | -10–0 | 0–10 | 10–20 | 20–32 | 32–40 | 40–50 | 50–60 | 60–70 | 70–80 | 80+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `#4a2a7a` | `#6c4fb0` | `#4560c8` | `#3f8fd2` | `#7fc6e8` | `#b7e2b1` | `#e6f2a2` | `#fee08b` | `#fdae61` | `#f46d43` | `#c2304a` |

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

### `meta` and tiles (snow depth, temperature)

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
`GET /status` has an item per layer (`snowdepth`, `temperature`, `radar`) that turns unhealthy
past the same limit as `stale`. `/readyz` does not depend on them.

Revisions not referenced by `latest.json` are deleted after 7 days. Nothing
needs backing up; to start over, delete `dem-cogs/<layer>/<region>/latest.json`
and run the job.

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
