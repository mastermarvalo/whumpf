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

## Next up

1. Smoke-test checklist against the real API (see `docs/PLAN.md`, Phase 0).
2. Remaining network-exposure items in `docs/PLAN.md`.
