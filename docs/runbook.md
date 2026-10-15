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
After changing what a tile endpoint returns, purge `/tiles/*`.

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
