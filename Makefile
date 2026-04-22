# -----------------------------------------------------------------------------
# Whumpf — convenience targets
#
# We use `docker compose` rather than `podman-compose` because compose v2 has
# much better coverage of the compose spec (healthcheck-based depends_on in
# particular). On Ubuntu 24.04 with Podman, this works by pointing Docker CLI
# at the Podman socket — see docs/runbook.md.
#
# To switch to `podman-compose`, override COMPOSE:
#   make up COMPOSE=podman-compose
# -----------------------------------------------------------------------------
COMPOSE ?= docker-compose

.DEFAULT_GOAL := help

.PHONY: help
help:  ## show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_.-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- lifecycle --------------------------------------------------------------

.PHONY: up
up:  ## build + start the full stack in the background
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

# --- health -----------------------------------------------------------------

.PHONY: health
health:  ## curl the readiness endpoint — quick "is everything wired up" check
	@curl -fsS http://localhost:8000/readyz | jq . || echo "api not reachable"

# --- tests / lint -----------------------------------------------------------

.PHONY: test-api
test-api:  ## run backend pytest inside the api container
	$(COMPOSE) exec api uv run pytest -q

.PHONY: lint-api
lint-api:  ## ruff check the backend
	$(COMPOSE) exec api uv run ruff check app tests
