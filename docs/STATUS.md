# Status

Last updated 2026-10-13.

## Phase 0: Stabilize — done

- Hermes latin1 `TextDecoder` shim added and imported first in the app layout; stray root `app/` folder and `README.txt` removed.
- Terrain tile retry: failed loads are no longer cached as null. Only successes and real 204 "no data" answers are cached, so errors retry on the next tap.
- No-signal handling: tile and point requests fail cleanly offline instead of hanging or caching the failure.
- Backend: aspect tile proxy endpoint added.

## Also done today

- Saved layer settings: per-layer choices persist across launches.
- Aspect layer on the map, with a legend. Other layers got legends too.
- Trip and waypoint rework: waypoints have icons and optional colors (backend column added); trip screens updated to match.
- Compass and follow-heading mode on the map.

## Refresh tokens — done

- `POST /auth/token` and `/auth/register` (skip-verification mode) return a `refresh_token`; `POST /auth/refresh` rotates it; `POST /auth/logout` with `{refresh_token}` revokes that device's chain. Cookie flow unchanged.
- Reusing a revoked token revokes all of the user's refresh tokens, except a token rotated under 60 s ago whose chain is still live, which gets a fresh token instead (lost responses, racing refreshes).
- Password reset revokes all; account deletion deletes the rows.

### Known gaps (later)

- **Access tokens survive revocation.** Access JWTs are stateless and keyed on email, so after a password reset, logout or reuse detection an already-issued access token stays valid for up to 60 minutes. Fix: a per-user `token_version` claim checked in `get_current_user`.
- **No cleanup of old rows.** Expired and revoked `refresh_tokens` rows are never deleted. Fine at current size; add a periodic delete of rows expired or revoked more than a few days ago.

## Next up

1. Smoke-test checklist against the real API (see `docs/PLAN.md`, Phase 0).
2. Remaining network-exposure items in `docs/PLAN.md`.
