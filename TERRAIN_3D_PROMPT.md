# 3D Terrain Feature — Claude Code Prompt

Add MapLibre 3D terrain to Whumpf using two RGB terrain tile sources — 10m base
and 1m hires — generated from existing COGs in MinIO.

---

## Pipeline

**File:** `data/pipelines/terrain_rgb_pipeline.py`

- Use `rio-rgbify` to convert:
  - `dem-cogs/{region}/dem.tif` → `terrain_rgb.mbtiles`
  - `dem-cogs/{region}/dem_hires.tif` → `terrain_rgb_hires.mbtiles`
- Upload both to MinIO bucket `dem-cogs` under:
  - `{region}/terrain_rgb.mbtiles`
  - `{region}/terrain_rgb_hires.mbtiles`
- Follow the pattern in `data/pipelines/dem_pipeline.py` for MinIO upload and
  CLI arg handling.

---

## Backend

**File:** `backend/app/routers/tiles.py`

- Add `GET /tiles/terrain_rgb/{z}/{x}/{y}`
  - Serves 10m MBTiles at z≤12, 1m hires MBTiles at z≥13
  - Read MBTiles from MinIO via vsicurl
  - LRU cache using the same `_cache_get` / `_cache_put` pattern as slope and
    contour endpoints
  - Return PNG with `Cache-Control: public, max-age=86400`
  - Use `call_with_resilience` and `CircuitOpenError` handling matching existing
    endpoints

---

## Frontend

**File:** `frontend/src/components/Map/layers/basemaps.ts`

- Add `getTerrainSource()` returning a MapLibre `raster-dem` source config
  pointed at `{API_URL}/tiles/terrain_rgb/{z}/{x}/{y}`
  - `tileSize: 256`
  - `encoding: "terrarium"` (rio-rgbify default)
  - `minzoom: 6`, `maxzoom: 16`

**File:** `frontend/src/components/Map.tsx`

- Add `terrain3d: boolean` state, default `false`
- On map init, add the terrain-rgb source immediately after other sources
  (always loaded, never removed — toggle only calls setTerrain on/off)
- On `terrain3d` change:
  - `true` → `map.setTerrain({ source: 'terrain-rgb', exaggeration: 1.0 })`
    and add a MapLibre `SkyLayer`
  - `false` → `map.setTerrain(null)` and remove the SkyLayer
- Do NOT trigger a style reload on toggle

**File:** `frontend/src/components/Map/ToolboxPanel.tsx`

- Add a 2D/3D toggle button
- Match existing button style exactly (same layout, aria-pressed, active state
  color treatment as Measure Slope and Slope Filter buttons)
- Pass `terrain3d` and `onTerrain3dToggle` as props from Map.tsx

---

## Constraints

- Terrain source is always loaded after map init — `setTerrain` is the only
  toggle mechanism, never remove/re-add the source
- Default state is flat (2D)
- Match all existing patterns exactly:
  - LRU cache → `_cache_get` / `_cache_put` / `OrderedDict`
  - Resilience → `call_with_resilience` + `CircuitOpenError`
  - Hires switchover → z13+ uses `_hires` variant (matches slope/contour/aspect)
  - vsicurl MinIO access → match `_build_dem_url` pattern in tiles.py
  - Button style → match ToolboxPanel existing buttons exactly
