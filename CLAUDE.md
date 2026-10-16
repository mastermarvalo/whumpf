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
    - `/healthz`, `/readyz`, and `GET /status` (the service status and banner page).
    - The `/auth/*` endpoints that sign you in or recover an account (register, token, refresh, verify-email, password-reset), each with `@limiter.limit`. The Strava OAuth callback is protected by its signed `state`.
- **Visibility enum is `private | shared | unlisted`.** Deliberately no `public`, no feed/discovery. New objects default to `private`.
- **Geometry in PostGIS**, GeoJSON at the API boundary.
- **Validate every `region` param** with `app/regions.py::validate_region`.
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
- New env vars go in `.env.example` with a comment.

## Where we are
- Phase 2 backend is complete (refresh tokens, 6-digit email codes, password reset, change password/email, delete with password, Strava `return_to`). Next is Phase 3 (Fix and verify; backend CI is done and green: ruff config plus fixes, 241 tests, old-site lockfile fixed. Remaining: Strava iPhone redirect, phone test of type-checked-only code, `/docs` off in prod, make `whumpf-ios` private). TestFlight is now Phase 7, the last; its backend part is likely serving the privacy policy, terms and credits pages. Not built yet.
- Retire with the old site: `STRAVA_RETURN_TO_ALLOW_DEV` and `TILES_ALLOW_TOKENLESS` must be off (`LEGACY_EMAIL_LINKS` too). Details in `docs/runbook.md`.
- Done: Alembic replaced `create_all` + `_SCHEMA_PATCHES`; prod is stamped at `0001_baseline`. Schema changes go through `make makemigration`; tests run on `whumpf_test` (`docs/runbook.md` §6).
- Done: our own ski run tiles at `/tiles/ski`, rebuilt weekly (Tuesdays 04:00) with automatic rollback (`docs/runbook.md` §7).
- Done: daily backups at 03:00 (local 14-day dump plus Cloudflare R2, read back and checksum-verified). Cloudflare caches `/tiles/*` (verified).
- Open work is in the "Later" list in `docs/PLAN.md`.

## Don't
- Don't introduce a second ORM, a second migration tool, or another web framework.
- Don't add public/feed/discovery/leaderboard features.
- Don't commit real secrets; `.env` is gitignored.
