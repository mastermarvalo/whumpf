# Whumpf — Route Planning, Trips & Party Sharing (implementation brief)

This brief describes a feature slice to build into the existing Whumpf codebase. Read
the existing code before writing anything — this brief names real symbols, but verify
them. Build in the phases given, and **stop at each checkpoint** so the work can be
reviewed before moving on.

---

## 1. Context: what already exists

Whumpf is a self-hosted backcountry terrain app. Stack: FastAPI + SQLAlchemy 2.x +
PostGIS, TiTiler/Martin for tiles, MinIO for object storage, a Vite + React + MapLibre
frontend. No Alembic — schema is managed by `Base.metadata.create_all()` in
`backend/app/main.py`'s `lifespan`, plus an idempotent `_SCHEMA_PATCHES` tuple of
`ALTER TABLE ... IF NOT EXISTS` statements that runs every startup.

Relevant existing pieces you will build on or alongside:

- **Auth**: `app/models/user.py` (`User`), `app/auth/dependencies.py::get_current_user`,
  email verification + password reset already wired. `app/admin.py::is_admin`.
- **DB**: `app/db.py::get_session` (FastAPI dependency, one session/request). Models
  register against `Base` in `app/models/__init__.py`.
- **Terrain analysis**: `app/services/cog_sampler.py::sample_profile(start, end, region,
  settings, n)` returns `list[TerrainSample]` (distance/elevation/slope/aspect) by
  reading slope/dem/aspect COGs over `/vsicurl`. `app/routers/terrain.py::/terrain/profile`
  wraps it and computes a `ProfileSummary` (slope & aspect distributions, gain/loss,
  min/max elevation) via `_summarise`. **This is currently hardwired to a single A→B
  segment.**
- **CAIC forecast/obs**: `app/routers/avalanche.py` caches the CAIC map-layer forecast
  (`get_forecast()`) and observation reports (`get_observations()`) server-side with
  stale-while-revalidate. Forecast features carry per-zone danger ratings.
- **Regions**: `app/regions.py` — hardcoded registry, currently only `colorado`.
  Validate every `region` param with `validate_region`.
- **Email**: `app/services/email.py` — pluggable provider, console default. Use it for
  party invites.
- **Frontend map**: `frontend/src/components/Map.tsx` + `frontend/src/components/Map/*`.
  The A→B measure tool lives in `MeasurePanel.tsx` and the tool buttons in
  `ToolboxPanel.tsx`. Shared types in `Map/types.ts`. URL-state sharing in
  `Map/urlState.ts`. Layer architecture: `LayerGroup`/`ActiveLayer` in `types.ts`,
  geojson layers added via helpers in `Map/layers/*` (see `strava.ts` for the pattern).
  Strava activities render as a colored geojson line layer — mirror that pattern for
  saved routes.

---

## 2. Hard constraints (do not deviate without asking)

1. **No `public` visibility anywhere.** Visibility enum is exactly
   `private | shared | unlisted`. `private` = owner only. `shared` = explicit party
   members / share-token holders. `unlisted` = anyone with the link token, never
   discoverable or indexed. Do **not** add a public/feed/discovery concept. This is a
   deliberate product decision to keep Whumpf a planning tool, not a social network.
2. **Private by default.** Every new object is created `private`. Sharing is always an
   explicit action by the owner.
3. **Authorization on every read and write.** A user may only see/modify objects they
   own, are a party member of, or hold a valid share token for. Enforce this at the
   query layer in the router, not just the UI. Add a small reusable helper rather than
   repeating ownership checks.
4. **Follow existing patterns.** SQLAlchemy 2.x `Mapped[...]` style (copy `user.py`).
   Register new models in `app/models/__init__.py`. Add any new columns on
   pre-existing tables to `_SCHEMA_PATCHES` in `main.py` as idempotent `IF NOT EXISTS`
   statements. New tables are created by `create_all` automatically. Routers go in
   `app/routers/` and are included in `main.py::create_app`.
5. **PostGIS geometry**, not lat/lng JSON columns, for route lines and observation/
   waypoint points. Use `geoalchemy2` (`Geometry("LINESTRINGZ", srid=4326)` /
   `Geometry("POINTZ"/"POINT", srid=4326)`). If `geoalchemy2` is not already a dep, add
   it to `backend/pyproject.toml`. Store/return geometry as GeoJSON at the API boundary.
6. **Keep the A→B measure tool as-is in spirit.** It becomes the dedicated *slope
   analysis* tool. Route drawing is a **separate** multi-vertex tool. Do not overload
   the measure tool with persistence.

---

## 3. Data model

Create these as new model files under `app/models/` and register them. Use timezone-aware
`DateTime(timezone=True)`, `created_at`/`updated_at`, integer PK autoincrement (matching
`User`).

### `routes` (reusable geometry — the keystone)
- `id`, `owner_id` (FK users), `name`, `notes` (text, default "")
- `region` (varchar, validated against the registry)
- `geom` — `LINESTRINGZ` 4326 (store elevation as Z per vertex)
- `summary` — JSONB: the `ProfileSummary` shape (avg/max/min slope, gain/loss,
  elevation range, slope_distribution, aspect_distribution)
- `samples` — JSONB: the full `list[SlopeSample]` (distance/elevation/slope/aspect per
  point). **Store this** — it's only a few KB and lets a saved route render its chart and
  histograms offline without re-reading COGs. (Decision: summary + geometry + cached
  samples all stored.)
- `visibility` — enum `private|shared|unlisted`, default `private`
- `share_token` — nullable varchar(64), indexed, set only when `unlisted`
- timestamps

### `trips` (a dated plan)
- `id`, `owner_id` (FK users), `name`, `date` (date), `region`
- `caic_zone` — nullable varchar (the CAIC zone id/name this trip is in)
- `forecast_snapshot` — JSONB, nullable: **frozen** copy of the relevant CAIC forecast
  (danger rating + avalanche problems for the zone) captured at trip creation/finalize.
  Never recompute — the point is "what we knew when we planned."
- `notes`, `visibility` (same enum, default `private`), timestamps
- A trip references one or more routes via a join table `trip_routes`
  (`trip_id`, `route_id`, optional `ordering` int).

### `trip_members` (party — membership with state)
- `id`, `trip_id` (FK), `user_id` (FK, nullable until invite accepted),
  `invited_email` (varchar — invite-by-email using `services/email.py`),
  `status` enum `invited|accepted|declined`, `role` enum `owner|member`,
  invited_at / responded_at. Owner is auto-added as `accepted|owner` on trip creation.

### `route_shares` (token-based, revocable, for routes)
- `id`, `route_id` (FK), `token` (varchar(64), unique, indexed),
  `created_by_id`, `created_at`, `revoked_at` (nullable).
- A valid (non-revoked) token grants **view + clone** on the route to anyone who holds
  it. Cloning = deep-copy the route (geom + summary + samples) into the requester's
  account as a new `private` route owned by them.

### `waypoints` (planning markers on a trip)
- `id`, `trip_id` (FK), `geom` (`POINTZ` 4326), `kind` enum
  (`parking|trailhead|transition|decision|summit|hazard|other`), `label`, `notes`,
  `created_by_id`, timestamps. Party-visible (inherits trip visibility/membership).

### `observations` (records of reality — separate from waypoints)
- `id`, `owner_id` (FK), `geom` (`POINT` 4326), optional `route_id` (FK, nullable),
  optional `trip_id` (FK, nullable), `observed_at` (datetime),
  `kind` enum (`whumpf|cracking|recent_avalanche|surface|wind_effect|other`),
  `notes`, `photo_object_key` (nullable varchar — MinIO object key),
  `visibility` (same enum, default `private`), timestamps.

### `route_comments` (flat, optional — build last)
- `id`, `route_id` (FK), `author_id` (FK), `body` (text), `created_at`.
  Flat list only — **no threads, no mentions, no notifications.** Visible to anyone who
  can view the route.

---

## 4. Backend API

New routers, all under `get_current_user` unless noted. Return geometry as GeoJSON.
Reuse a shared authorization helper (e.g. `app/auth/access.py`) with functions like
`can_view_route(user, route, session)`, `can_edit_route`, `assert_trip_member`.

**Routes** (`app/routers/routes.py`)
- `POST /routes` — create from a posted GeoJSON LineString + name + region. Server
  samples terrain for the line (see §5), computes summary, stores geom/summary/samples.
- `GET /routes` — list caller's own + routes shared to them (via party or share token
  presented as a query param).
- `GET /routes/{id}` — view (authorized). Also resolvable via `?token=` for `unlisted`.
- `PATCH /routes/{id}` — edit name/notes/visibility (owner only).
- `DELETE /routes/{id}` — owner only.
- `POST /routes/{id}/clone` — clone into caller's account (requires view access).
- `POST /routes/{id}/share` — create a `route_shares` token (owner only) → returns token.
- `DELETE /routes/{id}/share/{token}` — revoke (owner only).

**Trips** (`app/routers/trips.py`)
- CRUD for trips. On create, auto-add owner to `trip_members`, attach routes,
  capture `forecast_snapshot` from `avalanche.get_forecast()` filtered to `caic_zone`.
- `POST /trips/{id}/members` — invite by email (sends invite via `services/email.py`).
- `POST /trips/{id}/members/respond` — accept/decline (by invited user).
- `GET /trips/{id}` — full plan view: trip + routes + waypoints + frozen forecast +
  party roster. Authorized to members only.
- Waypoint CRUD nested under the trip.

**Observations** (`app/routers/observations.py`)
- CRUD. Photo upload → MinIO (mirror however existing uploads/bucketing is done; check
  the MinIO bootstrap and any existing upload code). Return a presigned or proxied URL.

**Comments** (optional, last) — `POST/GET/DELETE /routes/{id}/comments`.

Register all new routers in `main.py::create_app`. Add idempotent `_SCHEMA_PATCHES`
entries only for columns added to *existing* tables (new tables need none).

---

## 5. Multi-vertex terrain sampling (required for route drawing)

`sample_profile` currently takes a single `start`/`end` and samples `n` points along one
great-circle segment. Generalize it without breaking the existing A→B caller:

- Add a function that accepts an **ordered list of vertices** `[(lng,lat), ...]` and
  samples each leg, concatenating into one continuous profile with cumulative distance.
  Allocate sample points per-leg proportional to leg length (keep a sane total cap;
  respect the existing `_MAX_PROFILE_DISTANCE_M` over the summed length).
- Keep the existing two-point `sample_profile` working (the A→B measure tool still uses
  it) — either by keeping it and adding a new `sample_polyline`, or by making the old one
  a thin wrapper over the new.
- `_summarise` in `terrain.py` should work unchanged on the concatenated samples. Expose
  the polyline sampling either by extending `/terrain/profile` to accept a posted
  LineString, or via the `POST /routes` create path (which needs it anyway). Per-leg
  breakdown in the response is a nice-to-have, not required for v1.

---

## 6. Frontend

Follow existing conventions: types in `Map/types.ts`, geojson layer helpers in
`Map/layers/` (copy the `strava.ts` pattern for rendering saved routes as a colored
line layer), tool buttons in `ToolboxPanel.tsx`, panels as siblings to `MeasurePanel`.
State that should persist across devices lives on the server now — do not put routes/
trips in localStorage.

1. **Route builder tool** — a new toolbox button, separate from the A→B measure button.
   Click to drop ordered vertices on the map, building a polyline; show running
   distance. A "Save route" action posts the GeoJSON to `POST /routes` with a name +
   the current region, then renders it as a saved-route layer.
2. **A→B measure tool stays** as the dedicated *slope analysis* tool — unchanged behavior,
   just no persistence. (It already shows the slope/aspect histograms via `MeasurePanel`.)
3. **Saved routes layer + list** — a panel listing the user's routes and routes shared
   to them; toggling one renders its geometry + lets you open its stored profile
   (rendered from the cached `samples`, no re-fetch).
4. **Trip view** — a read-only plan summary: route(s) profile, waypoints, frozen CAIC
   forecast for the trip date/zone, and party roster. This is the trailhead view — keep
   it lightweight and consider it the thing most worth making work offline.
5. **Sharing UI** — owner can generate/revoke a share link for a route, and invite party
   members to a trip by email. Recipient of a shared route can **view + clone**.
6. **Observations** — drop a point, pick a kind, optional note + photo; renders as a
   layer. **Comments UI** (optional/last): a flat comment list on the route panel.

---

## 7. Build order & checkpoints

Build in this order and **pause at each checkpoint** for review:

- **Phase A — Persistence foundation.** `routes` model + multi-vertex sampling +
  `POST/GET/PATCH/DELETE /routes` + the access-control helper. Frontend: route builder
  tool + save + saved-routes layer/list. **Checkpoint: can draw, save, reload, and
  render a multi-vertex route, owner-only.**
- **Phase B — Sharing routes.** `route_shares` + share/revoke endpoints + clone +
  `unlisted` token resolution. Frontend: share-link UI + clone. **Checkpoint: a second
  account can open a shared link, view, and clone.**
- **Phase C — Trips & parties.** `trips`, `trip_routes`, `trip_members`, `waypoints`,
  forecast snapshot, email invites, trip view. **Checkpoint: create a trip with a route
  and frozen forecast, invite a member, member sees the plan.**
- **Phase D — Observations.** model + CRUD + MinIO photos + layer. **Checkpoint:
  observation with photo renders on the map.**
- **Phase E — Comments (optional).** Only if time remains. Flat, route-scoped.

Run the existing backend tests (`make test-api`) and add tests for the access-control
helper and the visibility rules specifically — the authorization logic is the part most
worth covering.

---

## 8. Non-goals (do not build these)

- No public feed, global activity stream, discovery, search-by-other-users, or profiles.
- No kudos / likes / leaderboards / "most-skied line" or any popularity signal.
- No real-time location sharing or live tracking.
- No comment threads, @mentions, or push/in-app notifications (email invites only).
- No GPX/KML import (export is fine later, not required now).
- No new auth providers, no Authentik, no payment.

If a task seems to require any of the above, stop and ask rather than building it.
