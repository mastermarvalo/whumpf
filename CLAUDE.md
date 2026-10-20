The roadmap and prompt list is in docs/PLAN.md. Read it when asked what's next or when starting a phase.

# Whumpf

Self-hosted backcountry terrain & trip-planning app. Backend: FastAPI + SQLAlchemy 2.x + PostGIS (`backend/`). Tiles: TiTiler (raster) + Martin (vector). Object storage: MinIO. Frontend: Vite + React + MapLibre (`frontend/`). Runs in containers via `compose.yml`, reverse-proxied by Nginx Proxy Manager.

## Hard rules
- **Schema changes go through Alembic** (`backend/migrations/`). Change the model, run `make makemigration m="..."`, read and fix the generated file (renames come out as drop+add; backfills are hand-written), then `make test-api`. Never edit `0001_baseline` or any applied migration; add a new one. No `create_all()` and no startup `ALTER` patches. `entrypoint.sh` runs `alembic upgrade head` on deploy. Details: `docs/runbook.md` §6.
- **Never point tests or destructive commands at the live DB.** Tests rebuild `whumpf_test` from migrations; `0001_baseline` downgrade refuses on any other DB.
- **Register every new model** in `app/models/__init__.py`.
- **SQLAlchemy 2.x style only** — `Mapped[...]` + `mapped_column(...)`, timezone-aware `DateTime(timezone=True)`. Reference: `app/models/user.py`.
- **No FK cascades and no ORM relationships.** Deleting a parent must explicitly delete child rows and `session.flush()` before deleting the parent (see `delete_trip`, `delete_route`); otherwise Postgres raises a FK violation (500).
- **One session per request** via `get_session` (`app/db.py`). No ad-hoc engines/sessions.
- **Auth on every read and write** via `get_current_user` (`app/auth/dependencies.py`). Ownership/membership/visibility is enforced in the router/query layer (`app/auth/access.py`), never only in the frontend. Admin checks: `app/admin.py::is_admin`.
  - Deliberate public exceptions (don't flag these; anything new needs the same reasoning and a limit):
    - `/tiles/*`: a tile token (`app/tile_tokens.py`) instead of sign-in. `POST /tiles/token` is open with a per-IP limit.
    - `/avalanche/*`: public CAIC data used by signed-out users and the old site. Per-IP limit (`avalanche` bucket in `tile_tokens.BUCKETS`).
    - `GET /weather/forecast` (spot forecast): public NWS data for signed-out installs too. Needs a tile token instead of sign-in (so it isn't an open proxy to api.weather.gov under our User-Agent), plus a per-IP limit (`weather` bucket, 60/min and 600/h).
    - `GET /cameras` and `GET /cameras/{id}/image` (mountain cameras): public NPS and Windy webcams for signed-out installs too. Needs a tile token instead of sign-in (so `/image` isn't a free anonymous way to spend our Windy key), plus a per-IP limit (`cameras` bucket, 60/min and 600/h) and a global cap on lookups that reach Windy (`windy_upstream`, 60/min and 1000/h).
    - `GET /roads` (road conditions): CDOT COtrip data for signed-out installs too. Needs a tile token instead of sign-in (it serves only the job's stored files and never calls CDOT, so the token is there to stop scraping), plus a per-IP limit (`roads` bucket, 60/min and 600/h). The segment lines are under `/tiles/roads/segments`.
    - `/healthz` and `/readyz`.
    - `GET /status` (the service status and banner page, polled by the app every 5 min). Per-IP limit (`status` bucket, 60/min and 600/h); the service checks are cached in memory for 30 s per worker, banners are read fresh so posts and resolves show at once.
    - The `/auth/*` endpoints that sign you in or recover an account (register, token, refresh, verify-email, password-reset), each with `@limiter.limit`. The Strava OAuth callback is protected by its signed `state`.
- **Offline packs (`/packs/*`) are signed-in only, shared and limited** (`docs/api-packs.md`, `docs/runbook.md` §12). A pack is shared by everyone who makes the same request (id = HMAC of region, rounded box, layers and source versions), so `GET /packs/{id}` must check `pack_requests`: anyone who didn't ask gets the same 404 as an unknown id. Each POST can start a build of up to ~3,000 tiles on the one pack worker, so: `packs` bucket 10/h per user (every POST counts), `packs_poll` 60/min and 1200/h per user for GETs, at most 2 of a user's packs queued or building (429) and 20 on the server (503 + Retry-After), the last two counted in Postgres under an advisory lock. Download links are presigned R2 GETs (24 h); never log them.
- **Visibility enum is `private | shared | unlisted`.** Deliberately no `public`, no feed/discovery. New objects default to `private`.
- **Geometry in PostGIS**, GeoJSON at the API boundary.
- **Validate every `region` param** with `app/regions.py::validate_region`.
- **The Cameras layer must never sit behind a paid tier.** Windy's free-tier terms forbid offering its webcams "solely in the paid part" of an app. Its images are never cached or rehosted either (`docs/runbook.md` §10).
- **Road conditions show only what CDOT says.** Map a CDOT value to a category (chain law, traction law, closed…) only after it has been seen in the real feed; everything else is `other` with CDOT's own text. Never guess from wording. CDOT's full disclaimer (`roads.DISCLAIMER`) must show wherever the data is shown. The COtrip key goes in the URL, so never log a URL or an httpx error message in that code (`docs/runbook.md` §11).
- **Stored secrets/tokens** use `EncryptedString` (`app/auth/encryption.py`, see `app/models/strava.py`).

## Conventions
- New routers go in `app/routers/` and are included in `app/main.py::create_app`.
- External HTTP calls go through `app/http_retry.py::call_with_resilience` (see `app/services/strava.py`, `routers/avalanche.py`).
- Email goes through `app/services/email.py` (pluggable); don't hardcode a provider.
- Frontend map: shared types in `frontend/src/components/Map/types.ts`; geojson layer helpers in `Map/layers/` (copy `layers/strava.ts`); tool buttons in `Map/ToolboxPanel.tsx`. Server-backed state lives on the server; only device preferences go in localStorage / URL state.
- Use `make <target>` for container ops, never raw podman/docker. The compose binary is `~/.docker/cli-plugins/docker-compose` (the `docker` on this host is the podman shim, which has no `compose`).
- Claude Code never restarts or deploys prod: no `make up`, `make restart-*` or `make nuke` against it. Change code, tests and docs; the owner deploys.

## Definition of done
- `make test-api` passes (it also fails if models and migrations differ). Add tests for any new authorization/visibility logic.
- `make health` returns `"ready": true`.
- New env vars go in `.env.example` with a comment, and every Settings field goes in `compose.yml`'s api `environment:` with the same default as `config.py`. `make check-env` (also in CI) fails otherwise.

## Where we are
- Phase 2 backend is complete (refresh tokens, 6-digit email codes, password reset, change password/email, delete with password, Strava `return_to`). Next is Phase 3 (Fix and verify; backend CI is done and green: ruff config plus fixes, 241 tests, old-site lockfile fixed. Remaining: Strava iPhone redirect, phone test of type-checked-only code, `/docs` off in prod, make `whumpf-ios` private). TestFlight is now Phase 7, the last; its backend part is likely serving the privacy policy, terms and credits pages. Not built yet.
- Retire with the old site: `STRAVA_RETURN_TO_ALLOW_DEV` and `TILES_ALLOW_TOKENLESS` must be off (`LEGACY_EMAIL_LINKS` too). Details in `docs/runbook.md`.
- Done: Alembic replaced `create_all` + `_SCHEMA_PATCHES`; prod is stamped at `0001_baseline`. Schema changes go through `make makemigration`; tests run on `whumpf_test` (`docs/runbook.md` §6).
- Done: our own ski run tiles at `/tiles/ski`, rebuilt weekly (Tuesdays 04:00) with automatic rollback (`docs/runbook.md` §7).
- Done: recent snowfall backend: NOHRSC 24 h/72 h at `/tiles/snowfall/*` (hourly `make snowfall-refresh` cron at :25, COGs in `dem-cogs/snowfall/`) and new snow on SNOTEL stations (`docs/runbook.md` §8). The app layer is a Phase 4 prompt.
- Done: weather backend (`docs/runbook.md` §9): snow depth (NOHRSC SNODAS, `/tiles/snowdepth/*`, hourly at :35), current temperature (NOAA RTMA, `/tiles/temperature/*`, hourly at :50), wind now (10 m RTMA wind from the same run, GeoJSON arrows at `/tiles/wind/*`; NBM forecast arrows are v1.1 in "Later"), radar (NOAA MRMS, 10-min frames for 2 h at `/tiles/radar/*`, every 5 min) and the spot forecast `GET /weather/forecast` (api.weather.gov). Shared raster layer code: `app/services/rasterlayer.py`. The app layers (and "From wind" moving to `/weather/forecast`) are Phase 4 prompts.
- Done: daily backups at 03:00 (local 14-day dump plus Cloudflare R2, read back and checksum-verified). Cloudflare caches `/tiles/*` (verified).
- Done: mountain cameras backend (`docs/runbook.md` §10): NPS (RMNP) and Windy webcam lists per region at `GET /cameras`, a fresh image per tap at `/cameras/{id}/image`, hourly `make cameras-refresh` cron at :10 (NPS every 6 h, Windy daily). Keys `NPS_API_KEY`, `WINDY_WEBCAMS_API_KEY`. CDOT cameras are tabled. The app layer is a Phase 4 prompt.
- Done: road conditions backend (`docs/runbook.md` §11): CDOT COtrip conditions per segment, incidents and planned events at `GET /roads`, simplified segment lines at `/tiles/roads/segments/<rev>.geojson`, `make roads-refresh` cron every 5 min (`2-59/5`). Key `COTRIP_API_KEY`; empty turns the layer off. `make roads-sample` (every 15 min for a week) records CDOT's values; chain law, traction law and avalanche control get mapped from it ("Later"). The app layer is a Phase 4 prompt.
- Done: offline pack tile cache, step B1 (`docs/runbook.md` §12): `app/services/pack_cache.py` makes or fetches each pack tile once (topo, imagery from USGS; slope, aspect, elevation, ski from the live endpoints' shared code in `app/services/terrain_tiles.py`) into SQLite under `/data/pack-cache/`, 100 GB LRU cap, `/status` item. `USGS_CONTACT` must be set for topo and imagery.
- Done: offline packs step B2, the pack worker (`docs/runbook.md` §12): `pack-worker` compose service (one process, advisory lock), `packs` table (migration `0002_packs`), one PMTiles file per layer into R2 at `packs/<id>/<layer>.pmtiles`, `make pack-queue` / `pack-list` / `logs-pack-worker`.
- Done: offline packs step B3, the endpoints (`docs/api-packs.md`, runbook §12): `GET /packs/manifest`, `POST /packs`, `GET /packs/{id}`, reuse by request id, rebuild after 29 days or when R2 lost a file, daily cleanup in the pack worker (`make pack-cleanup`), migration `0003_pack_requests`. Deployed and smoke-tested Oct 20, 2026: Berthoud Pass POST 202, ready in ~11 s from the cache, 72.7 MB in 6 files (the manifest estimate), downloaded MD5 matched, Range 206, second POST 200 ready at once. The R2 lifecycle rule (30 days, prefix `packs/`) is set by hand in the Cloudflare dashboard (§12). The app side is A4.
- Open work is in the "Later" list in `docs/PLAN.md`.

## Don't
- Don't introduce a second ORM, a second migration tool, or another web framework.
- Don't add public/feed/discovery/leaderboard features.
- Don't commit real secrets; `.env` is gitignored.
