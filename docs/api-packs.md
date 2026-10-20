# Offline packs API (B3)

The contract the app (A4) builds against. How packs are built and stored:
`docs/runbook.md` §12.

All endpoints need `Authorization: Bearer <access token>`. Signed out gets
`401 {"detail": "Not authenticated"}` (the standard `get_current_user`
answer). Errors are `{"detail": "<message>"}`; FastAPI's own 422s (a body
that doesn't parse) have a list in `detail`. Branch on the status code and
show `detail`. 429 and 503 always carry `Retry-After` (seconds).

## Limits

| Limit | Answer |
|---|---|
| 10 `POST /packs` an hour per user (every POST counts, even one answered ready) | 429 + Retry-After |
| 60 a minute and 1200 an hour per user for `GET /packs/{id}` and `GET /packs/manifest` together | 429 + Retry-After |
| At most 2 of your packs queued or building at once (joining someone else's build counts) | 429, Retry-After: 60 |
| At most 20 packs queued or building on the server; only when a request needs a new build | 503, Retry-After: 300 |

## `GET /packs/manifest?region=colorado`

**200**

```json
{
  "region": "colorado",
  "max_area_km2": 700,
  "max_side_km": 50,
  "grid_zoom": 14,
  "required_layers": ["topo", "slope", "aspect", "elevation", "ski"],
  "optional_layers": ["imagery"],
  "url_ttl_s": 86400,
  "limits": {"requests_per_hour": 10, "active_per_user": 2, "polls_per_minute": 60},
  "layers": [
    {"id": "topo", "minzoom": 8, "maxzoom": 16, "tile_type": "jpg", "tile_size": 256, "pad_tiles": 0,
     "attribution": "USGS The National Map", "source_version": null, "available": true,
     "bytes_per_tile": {"8": 36112, "9": 36562, "10": 33281, "11": 32625, "12": 28427, "13": 25476, "14": 20855, "15": 19662, "16": 13086}},
    {"id": "imagery", "minzoom": 8, "maxzoom": 16, "tile_type": "jpg", "tile_size": 256, "pad_tiles": 0,
     "attribution": "USGS The National Map: Orthoimagery (NAIP)", "source_version": null, "available": true,
     "bytes_per_tile": {"8": 23313, "9": 34912, "10": 31706, "11": 22701, "12": 20759, "13": 19534, "14": 22507, "15": 23627, "16": 24420}},
    {"id": "slope", "minzoom": 8, "maxzoom": 14, "tile_type": "png", "tile_size": 512, "pad_tiles": 0,
     "attribution": "USGS 3DEP", "source_version": "5d41402abc4b2a76b9719d911017c592", "available": true,
     "bytes_per_tile": {"8": 226833, "9": 229073, "10": 179967, "11": 180969, "12": 173784, "13": 166183, "14": 66930}},
    {"id": "aspect", "minzoom": 8, "maxzoom": 14, "tile_type": "png", "tile_size": 512, "pad_tiles": 0,
     "attribution": "USGS 3DEP", "source_version": "7d793037a0760186574b0282f2f435e7", "available": true,
     "bytes_per_tile": {"8": 263335, "9": 262466, "10": 208633, "11": 200384, "12": 187843, "13": 165162, "14": 65163}},
    {"id": "elevation", "minzoom": 8, "maxzoom": 14, "tile_type": "png", "tile_size": 256, "pad_tiles": 2,
     "encoding": "terrarium", "attribution": "USGS 3DEP",
     "source_version": "9e107d9d372bb6826bd81d3542a419d6|e4d909c290d0fb1ca068ffaddf22cbd0|ovr1", "available": true,
     "bytes_per_tile": {"8": 158352, "9": 155676, "10": 149604, "11": 143496, "12": 135133, "13": 124780, "14": 112535}},
    {"id": "ski", "minzoom": 8, "maxzoom": 14, "tile_type": "mvt", "tile_size": 512, "pad_tiles": 0,
     "attribution": "© OpenStreetMap contributors", "source_version": "1760852400123456789", "available": true,
     "bytes_per_tile": {"8": 1166, "9": 788, "10": 1065, "11": 3324, "12": 1105, "13": 688, "14": 174}}
  ]
}
```

- `layers` is in this order. `source_version` is null for the USGS layers
  (topo, imagery); treat the others as opaque strings.
- `available` is false for topo and imagery when the server has no USGS
  contact set, for every layer when R2 isn't configured, and for a layer
  whose version can't be read right now. `POST /packs` then answers 503.
- `bytes_per_tile` is the average size of a tile asked for at that zoom,
  empty tiles counting 0, from recent builds (seeded with a Berthoud Pass
  build). Keys are zooms as strings.

**Size estimate** (matches the server's tile count):

1. The zoom 14 tiles covering the box: x from the west edge to the east
   edge, y from the north edge to the south edge, Web Mercator. An edge
   exactly on a tile edge doesn't pull in the next row or column.
2. Add `pad_tiles` tiles on every side (elevation: 2).
3. For each zoom `z` from `minzoom` to `maxzoom`, with `d = |z − 14|`:
   below 14 the range is `x0 >> d … x1 >> d`; above 14 it is
   `x0 << d … ((x1 + 1) << d) − 1`; the same for y.
4. Layer size ≈ Σ (tiles at z) × `bytes_per_tile[z]`. Files store identical
   tiles once, so the real size is usually a little smaller.

Berthoud Pass (`[-105.835, 39.753, -105.719, 39.843]`) comes to about 73 MB
with imagery: topo 13.9, imagery 22.0, slope 8.6, aspect 8.8, elevation
19.3, ski 0.0.

**Errors:** 400 `"Unknown region: <region>"`, 403 `"Region not available
for this account"`, 429.

## `POST /packs`

```json
{"region": "colorado", "bbox": [-105.835, 39.753, -105.719, 39.843],
 "layers": ["topo", "imagery", "slope", "aspect", "elevation", "ski"]}
```

`bbox` is `[west, south, east, north]` in degrees. `layers` must hold topo,
slope, aspect, elevation and ski, and may add imagery; order doesn't
matter.

The same request (same region, same box after rounding to the zoom 14 grid,
same layers, same source versions) gives the same pack id, for any user.

- **202**: this request queued a build (a new pack, or one that failed or
  whose files had expired). Body: the pack object.
- **200**: the pack was already queued, building or ready. Body: the pack
  object; when ready it has the download URLs.

**Errors:**

| Status | `detail` |
|---|---|
| 400 | `"Unknown region: <region>"` |
| 403 | `"Region not available for this account"` |
| 422 | `"bbox must be four numbers: west, south, east, north"` |
| 422 | `"bbox must have west < east and south < north"` |
| 422 | `"bbox must be inside the colorado region"` |
| 422 | `"bbox is 31.2 x 25.0 km (780 km²); the most is 700 km²"` |
| 422 | `"bbox is 60.0 x 5.0 km; the most is 50 km on a side"` |
| 422 | `"layers must include topo, slope, aspect, elevation and ski; imagery is optional"` |
| 429 | `"Too many requests"` (10 an hour) |
| 429 | `"You already have 2 packs being prepared. Try again when one is ready."` |
| 503 | `"Too many packs are being prepared right now. Try again in a few minutes."` |
| 503 | `"Offline packs are not available right now."` |

## `GET /packs/{id}`

**200**: the pack object.

Queued:

```json
{"id": "3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14", "status": "queued", "region": "colorado",
 "bounds": [-105.84229, 39.74099, -105.71045, 39.85915],
 "layers": ["topo", "imagery", "slope", "aspect", "elevation", "ski"],
 "progress": 0.0, "tiles_done": 0, "tiles_total": 2218, "queue_position": 3,
 "error": null, "built_at": null, "urls_expire_at": null, "files": []}
```

Building:

```json
{"id": "3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14", "status": "building", "region": "colorado",
 "bounds": [-105.84229, 39.74099, -105.71045, 39.85915],
 "layers": ["topo", "imagery", "slope", "aspect", "elevation", "ski"],
 "progress": 0.366, "tiles_done": 812, "tiles_total": 2218, "queue_position": null,
 "error": null, "built_at": null, "urls_expire_at": null, "files": []}
```

Failed:

```json
{"id": "3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14", "status": "failed", "region": "colorado",
 "bounds": [-105.84229, 39.74099, -105.71045, 39.85915],
 "layers": ["topo", "imagery", "slope", "aspect", "elevation", "ski"],
 "progress": 0.0, "tiles_done": 0, "tiles_total": 2218, "queue_position": null,
 "error": "This pack couldn't be built. Try again later.",
 "built_at": null, "urls_expire_at": null, "files": []}
```

Ready (two of the six files shown):

```json
{"id": "3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14", "status": "ready", "region": "colorado",
 "bounds": [-105.84229, 39.74099, -105.71045, 39.85915],
 "layers": ["topo", "imagery", "slope", "aspect", "elevation", "ski"],
 "progress": 1.0, "tiles_done": 2218, "tiles_total": 2218, "queue_position": null, "error": null,
 "built_at": "2026-10-20T06:12:09Z", "urls_expire_at": "2026-10-21T14:03:51Z",
 "files": [
   {"layer": "topo", "version": null, "bytes": 13512044, "md5": "0cc175b9c0f1b6a831c399e269772661",
    "url": "https://<account>.r2.cloudflarestorage.com/<bucket>/packs/3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14/topo.pmtiles?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=…&X-Amz-Date=…&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&X-Amz-Signature=…"},
   {"layer": "ski", "version": "1760852400123456789", "bytes": 0, "md5": null, "url": null}
 ]}
```

**Errors:** 404 `"Pack not found"` (no such pack, or you never requested
it), 429, 503 (as for POST, when the pack has to be queued again; or
`"Offline packs are not available right now."` when R2 can't be reached).

## Notes for the app

- `status` is `queued | building | ready | failed`. `progress` is 0–1
  (three decimals). `queue_position` is 1 for the next pack, null unless
  queued. Every timestamp is UTC with a `Z`.
- `bounds` is the box rounded out to the zoom 14 grid; every layer covers
  it, and elevation also covers 2 more zoom 14 tiles on every side.
- `files` is empty unless `ready`. One entry per layer, in `layers` order.
  Each file is PMTiles v3, `application/vnd.pmtiles`. A layer with no tiles
  (for example no ski runs in the box) has `bytes` 0, `md5` null and `url`
  null: save no file for it.
- URLs are presigned R2 GETs, valid 24 h (`urls_expire_at`), made fresh on
  every call. They support `Range`. If one expires mid-download (403 from
  R2), call `GET /packs/{id}` again and resume with a Range request. **If
  the file's `md5` has changed since the download started, start that file
  again.** Check each finished file against `md5`.
- A `ready` pack can come back `queued`: its files expired (R2 keeps them
  30 days; the server rebuilds after 29), and the GET or POST queued it
  again. Throw away a partial download and poll again.
- Poll `GET /packs/{id}` about every 5 s while building and every 15 s
  while queued; the limit is 60 a minute.
- The same request from another user can be answered `ready` at once; that
  is the shared pack, not an error.
- The server forgets a pack 60 days after it was last requested (a failed
  one after 1 day); then `GET` is 404 and a new `POST` starts again.
