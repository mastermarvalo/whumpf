The roadmap and prompt list is in docs/PLAN.md. Read it when asked what's next or when starting a phase.

# Whumpf

Self-hosted backcountry terrain & trip-planning app. Backend: FastAPI + SQLAlchemy 2.x + PostGIS (`backend/`). Tiles: TiTiler (raster) + Martin (vector). Object storage: MinIO. Frontend: Vite + React + MapLibre (`frontend/`). Runs in containers via `compose.yml`, reverse-proxied by Nginx Proxy Manager.

## Hard rules
- **No Alembic.** Schema comes from `Base.metadata.create_all()` in `backend/app/main.py`. Columns added to *existing* tables go in the idempotent `_SCHEMA_PATCHES` tuple (`ALTER TABLE ... IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`). New tables need no patch.
- **Register every new model** in `app/models/__init__.py`.
- **SQLAlchemy 2.x style only** — `Mapped[...]` + `mapped_column(...)`, timezone-aware `DateTime(timezone=True)`. Reference: `app/models/user.py`.
- **No FK cascades and no ORM relationships.** Deleting a parent must explicitly delete child rows and `session.flush()` before deleting the parent (see `delete_trip`, `delete_route`); otherwise Postgres raises a FK violation (500).
- **One session per request** via `get_session` (`app/db.py`). No ad-hoc engines/sessions.
- **Auth on every read and write** via `get_current_user` (`app/auth/dependencies.py`). Ownership/membership/visibility is enforced in the router/query layer (`app/auth/access.py`), never only in the frontend. Admin checks: `app/admin.py::is_admin`.
- **Visibility enum is `private | shared | unlisted`.** Deliberately no `public`, no feed/discovery. New objects default to `private`.
- **Geometry in PostGIS**, GeoJSON at the API boundary.
- **Validate every `region` param** with `app/regions.py::validate_region`.
- **Stored secrets/tokens** use `EncryptedString` (`app/auth/encryption.py`, see `app/models/strava.py`).

## Conventions
- New routers go in `app/routers/` and are included in `app/main.py::create_app`.
- External HTTP calls go through `app/http_retry.py::call_with_resilience` (see `app/services/strava.py`, `routers/avalanche.py`).
- Email goes through `app/services/email.py` (pluggable); don't hardcode a provider.
- Frontend map: shared types in `frontend/src/components/Map/types.ts`; geojson layer helpers in `Map/layers/` (copy `layers/strava.ts`); tool buttons in `Map/ToolboxPanel.tsx`. Server-backed state lives on the server; only device preferences go in localStorage / URL state.
- Use `make <target>` for container ops, never raw podman/docker.

## Definition of done
- `make test-api` passes. Add tests for any new authorization/visibility logic.
- `make health` returns `"ready": true`.
- New env vars go in `.env.example` with a comment.

## Don't
- Don't introduce Alembic, a second ORM, or another web framework.
- Don't add public/feed/discovery/leaderboard features.
- Don't commit real secrets; `.env` is gitignored.
