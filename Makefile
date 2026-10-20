# -----------------------------------------------------------------------------
# Whumpf — convenience targets
#
# We use `docker compose` rather than `podman-compose` because compose v2 has
# much better coverage of the compose spec (healthcheck-based depends_on in
# particular). On Ubuntu 24.04 with Podman, this works by pointing Docker CLI
# at the Podman socket — see docs/runbook.md.
#
# On this host `docker` is the podman shim and doesn't support `docker compose`
# (no compose subcommand in podman). Invoke the v2 plugin binary directly.
# To switch to podman-compose: make up COMPOSE=podman-compose
# -----------------------------------------------------------------------------
COMPOSE ?= $(HOME)/.docker/cli-plugins/docker-compose

.DEFAULT_GOAL := help

.PHONY: help
help:  ## show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_.-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- lifecycle --------------------------------------------------------------

.PHONY: up
up:  ## build + start the full stack in the background
	@docker ps -aq --filter "name=whumpf-" | xargs -r docker rm -f 2>/dev/null; true
	$(COMPOSE) up -d --build

.PHONY: up-fg
up-fg:  ## start the full stack in the foreground (useful for debugging)
	$(COMPOSE) up --build

.PHONY: down
down:  ## stop the stack (keeps volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke:  ## stop the stack AND delete all volumes — destroys DB and MinIO data
	$(COMPOSE) down -v

.PHONY: build-api
build-api:  ## build the api image without restarting the running api
	$(COMPOSE) build api

.PHONY: stop-api
stop-api:  ## stop only the api (e.g. before make db-restore); make up starts it again
	$(COMPOSE) stop api

.PHONY: stop-pack-worker
stop-pack-worker:  ## stop only the pack worker (a build in progress is queued again); make up starts it again
	$(COMPOSE) stop pack-worker

.PHONY: restart
restart:  ## restart all services without rebuilding
	$(COMPOSE) restart

.PHONY: ps
ps:  ## list running containers
	$(COMPOSE) ps

# --- logs -------------------------------------------------------------------

.PHONY: logs
logs:  ## tail all logs
	$(COMPOSE) logs -f --tail=100

.PHONY: logs-api
logs-api:  ## tail the api logs only
	$(COMPOSE) logs -f --tail=200 api

.PHONY: logs-pack-worker
logs-pack-worker:  ## tail the pack worker logs (per-layer times, sizes, USGS made vs cached)
	$(COMPOSE) logs -f --tail=200 pack-worker

.PHONY: logs-db
logs-db:  ## tail the postgis logs
	$(COMPOSE) logs -f --tail=200 postgis

# --- shells -----------------------------------------------------------------

.PHONY: psql
psql:  ## open a psql prompt against the running whumpf DB
	$(COMPOSE) exec postgis psql -U $${POSTGRES_USER:-whumpf} -d $${POSTGRES_DB:-whumpf}

.PHONY: shell-api
shell-api:  ## shell into the api container
	$(COMPOSE) exec api /bin/bash

.PHONY: shell-minio
shell-minio:  ## shell into the minio container
	$(COMPOSE) exec minio /bin/sh

# --- database / migrations -------------------------------------------------
# These run in a one-off container from the current api image (`compose run`),
# never by exec'ing into the running api — so after `make build-api` they use
# the new code and migrations while the old api keeps serving. --no-deps: the
# database must already be up. ALLOW_DESTRUCTIVE_DOWNGRADE=1 can be passed on
# the make command line for the rare intentional baseline downgrade.

API_RUN = $(COMPOSE) run --rm --no-deps -T -e ALLOW_DESTRUCTIVE_DOWNGRADE=$(ALLOW_DESTRUCTIVE_DOWNGRADE) api

.PHONY: migrate
migrate:  ## apply pending schema migrations (alembic upgrade head)
	$(API_RUN) uv run alembic upgrade head

.PHONY: makemigration
makemigration:  ## autogenerate a migration from model changes: make makemigration m="add foo to bar"
	@test -n "$(m)" || { echo 'usage: make makemigration m="describe the change"'; exit 1; }
	$(API_RUN) uv run alembic revision --autogenerate -m "$(m)"

.PHONY: alembic
alembic:  ## run any alembic command: make alembic ARGS="current"
	$(API_RUN) uv run alembic $(ARGS)

.PHONY: db-check
db-check:  ## fail if the models and the database schema differ (alembic check)
	$(API_RUN) uv run alembic check

.PHONY: schema-diff
schema-diff:  ## diff the live schema against a scratch DB built from migrations
	COMPOSE="$(COMPOSE)" ./scripts/schema-diff.sh

BACKUP_DIR ?= /data/backups

.PHONY: db-dump
# DUMP_NAME= fixes the file name (scripts/backup.sh uses it to upload the same file).
db-dump:  ## local pg_dump (custom format) to $(BACKUP_DIR)/whumpf-<utc>.dump, then verify it reads back
	@mkdir -p $(BACKUP_DIR)
	@set -e; f=$${DUMP_NAME:-whumpf-$$(date -u +%Y%m%dT%H%M%SZ).dump}; \
	$(COMPOSE) exec -T postgis pg_dump -U $${POSTGRES_USER:-whumpf} -d $${POSTGRES_DB:-whumpf} -Fc -f /tmp/$$f; \
	$(COMPOSE) exec -T postgis sh -c "pg_restore --list /tmp/$$f | grep -c 'TABLE DATA' | sed 's/^/tables with data: /'"; \
	want=$$($(COMPOSE) exec -T postgis sha256sum /tmp/$$f | cut -d' ' -f1); \
	$(COMPOSE) cp postgis:/tmp/$$f $(BACKUP_DIR)/$$f.partial; \
	$(COMPOSE) exec -T postgis rm -f /tmp/$$f; \
	got=$$(sha256sum $(BACKUP_DIR)/$$f.partial | cut -d' ' -f1); \
	if [ "$$got" != "$$want" ]; then echo "db-dump: copy out of the container is corrupt ($$got != $$want)"; rm -f $(BACKUP_DIR)/$$f.partial; exit 1; fi; \
	mv $(BACKUP_DIR)/$$f.partial $(BACKUP_DIR)/$$f; ls -lh $(BACKUP_DIR)/$$f; echo "sha256 $$got"

.PHONY: db-restore
db-restore:  ## restore a db-dump: make db-restore FILE=/data/backups/x.dump CONFIRM=yes (stop the api first)
	@test -f "$(FILE)" || { echo "FILE=<path to .dump> required"; exit 1; }
	@test "$(CONFIRM)" = yes || { echo "This overwrites the live database. Re-run with CONFIRM=yes."; exit 1; }
	$(COMPOSE) cp $(FILE) postgis:/tmp/restore.dump
	$(COMPOSE) exec -T postgis pg_restore -U $${POSTGRES_USER:-whumpf} -d $${POSTGRES_DB:-whumpf} --clean --if-exists --no-owner /tmp/restore.dump
	$(COMPOSE) exec -T postgis rm -f /tmp/restore.dump

# --- health -----------------------------------------------------------------

.PHONY: health
health:  ## curl the readiness endpoint — quick "is everything wired up" check
	@curl -fsS http://localhost:8000/readyz | python3 -m json.tool || echo "api not reachable"

# --- DEM pipeline -----------------------------------------------------------

DEM_WORKDIR  ?= /data/dem-work
DEM_BBOX     ?= -109.06,37.0,-102.05,41.0
DEM_HIRES_BBOX ?= -109.06,37.0,-105.5,41.0
DEM_FLAGS    ?= --priority-stack
DEM_HIRES_MAX_GB ?= 700

PYTHON ?= .venv/bin/python

.PHONY: dem-pipeline
dem-pipeline:  ## run the DEM pipeline; override with DEM_BBOX=, DEM_HIRES_BBOX=, DEM_WORKDIR=, DEM_FLAGS=
	$(PYTHON) data/pipelines/dem_pipeline.py --workdir $(DEM_WORKDIR) --region colorado --bbox="$(DEM_BBOX)" --hires-bbox="$(DEM_HIRES_BBOX)" --hires-max-gb=$(DEM_HIRES_MAX_GB) $(DEM_FLAGS)

.PHONY: dem-pipeline-test
dem-pipeline-test:  ## quick smoke-test (0.5°×0.5° bbox near Silverton, ~2 min)
	$(PYTHON) data/pipelines/dem_pipeline.py --test --workdir $(DEM_WORKDIR) --region colorado

.PHONY: dem-clean-cogs
dem-clean-cogs:  ## delete processed COGs so the next pipeline run recomputes from downloaded tiles
	rm -rf $(DEM_WORKDIR)/cog

# --- ski run tiles ------------------------------------------------------------
# OSM extract -> tilemaker -> $(SKI_TILES_DIR)/ski_<region>.mbtiles, served by
# Martin and proxied at /tiles/ski. Weekly via cron: docs/runbook.md "Ski run tiles".
# SKI_WORKDIR must be on the same filesystem as SKI_TILES_DIR (atomic swap).

REGION        ?= colorado
SKI_WORKDIR   ?= /data/ski-work
SKI_TILES_DIR ?= /data/martin-tiles
CONTAINER     ?= podman
# Only a `master` tag is published; pin the digest and bump it deliberately.
TILEMAKER_IMAGE ?= ghcr.io/systemed/tilemaker@sha256:d3eda2790458de727bb0096eebce775984fc060f60901dac6dbfbbb79ae17370
SKI = $(PYTHON) data/pipelines/ski_tiles.py --region $(REGION) --workdir $(SKI_WORKDIR) --tiles-dir $(SKI_TILES_DIR)

.PHONY: restart-martin
restart-martin:  ## restart Martin so it picks up new or replaced .mbtiles files
	$(COMPOSE) restart martin

.PHONY: ski-tiles-fetch
ski-tiles-fetch:  ## download the region's OSM extract (skipped if the md5 is unchanged)
	$(SKI) fetch

.PHONY: ski-tiles-build
ski-tiles-build:  ## run tilemaker on the downloaded extract into $(SKI_WORKDIR)/out
	mkdir -p $(SKI_WORKDIR)/out
	$(CONTAINER) run --rm --network none \
		-v $(SKI_WORKDIR)/$(REGION)/download:/in:ro \
		-v $(CURDIR)/data/pipelines/ski:/cfg:ro \
		-v $(SKI_WORKDIR)/out:/out \
		$(TILEMAKER_IMAGE) --quiet \
		--input /in/$$(basename $$(ls $(SKI_WORKDIR)/$(REGION)/download/*.osm.pbf)) \
		--output /out/ski_$(REGION).mbtiles.tmp.mbtiles \
		--bbox $$($(SKI) bbox) \
		--config /cfg/config.json --process /cfg/process.lua
	mv $(SKI_WORKDIR)/out/ski_$(REGION).mbtiles.tmp.mbtiles $(SKI_WORKDIR)/out/ski_$(REGION).mbtiles

.PHONY: ski-tiles
ski-tiles: ski-tiles-fetch ski-tiles-build  ## fetch + build + validate (does not touch what is served)
	$(SKI) validate

.PHONY: ski-tiles-publish
ski-tiles-publish:  ## validate the build, swap it into $(SKI_TILES_DIR), restart Martin; rolls back on failure
	$(SKI) publish

.PHONY: ski-tiles-refresh
ski-tiles-refresh: ski-tiles ski-tiles-publish  ## the weekly job: fetch, build, validate, publish

# --- recent snowfall ---------------------------------------------------------
# NOAA NOHRSC 24 h / 72 h snowfall -> dem-cogs/snowfall/<region>/ in MinIO,
# served at /tiles/snowfall. Hourly via cron: docs/runbook.md "Recent snowfall".

.PHONY: snowfall-refresh
snowfall-refresh:  ## fetch the newest NOHRSC 24 h/72 h snowfall into MinIO (the hourly job); ARGS=--dry-run writes nothing
	$(API_RUN) uv run python -m app.jobs.snowfall_refresh $(ARGS)

# --- weather layers ----------------------------------------------------------
# NOAA snow depth, temperature and radar -> dem-cogs/<layer>/<region>/ in
# MinIO, served under /tiles. Cron lines: docs/runbook.md "Weather layers".
# ARGS=--dry-run downloads and validates but writes nothing.

.PHONY: snowdepth-refresh
snowdepth-refresh:  ## fetch the newest SNODAS snow depth into MinIO (hourly job); ARGS=--dry-run writes nothing
	$(API_RUN) uv run python -m app.jobs.snowdepth_refresh $(ARGS)

.PHONY: temperature-refresh
temperature-refresh:  ## fetch the newest RTMA 2 m temperature and 10 m wind into MinIO (hourly job); ARGS=--dry-run writes nothing
	$(API_RUN) uv run python -m app.jobs.temperature_refresh $(ARGS)

.PHONY: radar-refresh
radar-refresh:  ## fill missing 10-min MRMS radar frames in MinIO (every-5-min job); ARGS=--dry-run writes nothing
	$(API_RUN) uv run python -m app.jobs.radar_refresh $(ARGS)

# --- mountain cameras ---------------------------------------------------------
# NPS and Windy webcam lists -> dem-cogs/cameras/<region>/<source>.json in
# MinIO, served at /cameras. Hourly via cron: docs/runbook.md "Mountain cameras".

.PHONY: cameras-refresh
cameras-refresh:  ## refresh the camera lists that are due (hourly job); ARGS=--force asks every source now, ARGS=--dry-run writes nothing
	$(API_RUN) uv run python -m app.jobs.cameras_refresh $(ARGS)

# --- road conditions ----------------------------------------------------------
# CDOT COtrip conditions, incidents, planned events and segment lines ->
# dem-cogs/roads/<region>/ in MinIO, served at /roads. Every 5 min via cron:
# docs/runbook.md "Road conditions".

.PHONY: roads-refresh
roads-refresh:  ## refresh the CDOT road feeds that are due (5-min job); ARGS=--force asks every feed now, ARGS=--dry-run writes nothing
	$(API_RUN) uv run python -m app.jobs.roads_refresh $(ARGS)

.PHONY: roads-sample
roads-sample:  ## record the distinct CDOT condition/incident/impact values (15-min cron for a week); ARGS=--report prints them
	$(API_RUN) uv run python -m app.jobs.roads_sample $(ARGS)

# --- offline packs ------------------------------------------------------------
# The pack-worker service builds queued packs into R2: docs/runbook.md §12.

.PHONY: pack-queue
pack-queue:  ## queue an offline pack: make pack-queue ARGS="--bbox=W,S,E,N [--region colorado] [--layers topo,slope]"
	$(API_RUN) uv run python -m app.jobs.pack_worker queue $(ARGS)

.PHONY: pack-list
pack-list:  ## the newest offline packs: status, progress, files, errors
	$(API_RUN) uv run python -m app.jobs.pack_worker list $(ARGS)

.PHONY: pack-build-once
pack-build-once:  ## build the oldest queued pack in the foreground, then exit (only while the pack-worker service is stopped)
	$(COMPOSE) run --rm --no-deps -T pack-worker uv run python -m app.jobs.pack_worker once

# --- tests / lint -----------------------------------------------------------

.PHONY: test-api
test-api:  ## run backend pytest in a one-off api container (uses the whumpf_test DB)
	$(API_RUN) uv run pytest -q

.PHONY: lock-api
lock-api:  ## update backend/uv.lock after editing pyproject.toml (then make build-api)
	$(COMPOSE) run --rm --no-deps -T -v $(CURDIR)/backend:/src:Z -w /src api uv lock

.PHONY: check-env
check-env:  ## fail if a setting in .env.example or config.py isn't passed through compose.yml (also in CI)
	python3 scripts/check_env_passthrough.py
	python3 -m unittest discover -s scripts -p "test_check_env*.py"

.PHONY: lint-api
lint-api:  ## ruff check the backend
	$(API_RUN) uv run ruff check app tests migrations

# --- backups ---------------------------------------------------------------

.PHONY: backup
backup:  ## dump postgres + user-uploads to BACKUP_S3_* (see docs/external-services.md)
	./scripts/backup.sh
