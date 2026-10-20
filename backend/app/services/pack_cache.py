"""Offline pack tile cache (docs/runbook.md §12).

The pack worker reads every tile of an offline pack through
PackCache.get_tile: a stored tile if it is still good, else the tile is made
(or fetched), stored and returned. One SQLite file per layer under
PACK_CACHE_DIR, written only by the pack worker.

- topo, imagery: USGS basemap tiles, JPEG as received. Fetched once and kept
  while stored; a 404 (USGS has no tile there) is stored as empty and asked
  again after 30 days. At most 4 USGS requests at a time, User-Agent with
  USGS_CONTACT, retried on errors, 429 and 5xx. Failures are never stored.
- slope, aspect, elevation, ski: made by our own code, the same as the live
  /tiles endpoints (services/terrain_tiles.py), at most 2 at a time in their
  own pool so packs don't slow the live map. Each carries a source_version
  (MinIO ETag of the COGs, ski file mtime); a tile of an old version is made
  again.
- Above PACK_CACHE_MAX_GB the least recently used tiles, across all layers,
  are evicted down to 90% of the cap.

The concurrency limits hold within one process: the pack worker must run as
a single process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from app.config import get_settings
from app.http_retry import CircuitOpenError, call_with_resilience
from app.regions import validate_region
from app.services import terrain_tiles

logger = logging.getLogger("whumpf.pack_cache")

GB = 10**9

USGS_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/{service}/MapServer/tile/{z}/{y}/{x}"
USGS_CONCURRENCY = 4
OWN_CONCURRENCY = 2
# A 404 from USGS is kept this long, so a brief USGS problem can't leave a
# permanent hole in later packs.
USGS_EMPTY_TTL_S = 30 * 86400
RETRY_AFTER_MAX_S = 60.0

# Default home of ski_<region>.mbtiles (Makefile SKI_TILES_DIR).
SKI_DIR = Path("/data/martin-tiles")

EVICT_EVERY_BYTES = 256 * 1024**2  # check the cap after this much is written
EVICT_TO = 0.9                     # evict down to this share of the cap


@dataclass(frozen=True)
class Layer:
    name: str
    kind: Literal["usgs", "colormap", "elevation", "ski"]
    minzoom: int
    maxzoom: int
    tile_type: Literal["jpg", "png", "mvt"]
    usgs_service: str | None = None


LAYERS: dict[str, Layer] = {layer.name: layer for layer in (
    Layer("topo", "usgs", 8, 16, "jpg", "USGSTopo"),
    Layer("imagery", "usgs", 8, 16, "jpg", "USGSImageryOnly"),  # NAIP
    Layer("slope", "colormap", 8, 14, "png"),   # 512 px, as /tiles/slope (no hires)
    Layer("aspect", "colormap", 8, 14, "png"),  # 512 px, as /tiles/aspect
    Layer("elevation", "elevation", 8, 14, "png"),  # 256 px Terrarium, PNG level 9
    Layer("ski", "ski", 8, 14, "mvt"),          # gzipped MVT as stored
)}


class PackTileError(Exception):
    """A tile could not be made or fetched. Nothing was stored."""


# ── tile listing ───────────────────────────────────────────────────────────────

_MAX_LAT = 85.0511287798


def _tile_at(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 1 << z
    lat = max(-_MAX_LAT, min(_MAX_LAT, lat))
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in_bbox(
    bbox: tuple[float, float, float, float], minzoom: int, maxzoom: int,
) -> Iterator[tuple[int, int, int]]:
    """XYZ tiles (z, x, y) covering bbox (west, south, east, north) at
    minzoom..maxzoom."""
    west, south, east, north = bbox
    for z in range(minzoom, maxzoom + 1):
        x0, y0 = _tile_at(west, north, z)
        x1, y1 = _tile_at(east, south, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


# ── storage ────────────────────────────────────────────────────────────────────

def file_name(layer: Layer, region: str) -> str:
    """USGS layers are the same everywhere; ours are per region."""
    return f"{layer.name}.sqlite" if layer.kind == "usgs" else f"{layer.name}_{region}.sqlite"


def disk_usage(cache_dir: Path) -> int:
    """Bytes on disk of the cache's SQLite files (with WAL); 0 if none yet."""
    if not cache_dir.is_dir():
        return 0
    return sum(p.stat().st_size for p in cache_dir.glob("*.sqlite*") if p.is_file())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    z INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    data BLOB NOT NULL,            -- empty: a real empty answer
    made_at INTEGER NOT NULL,      -- unix seconds
    source_version TEXT,           -- NULL for USGS tiles
    last_used_at INTEGER NOT NULL, -- unix seconds
    PRIMARY KEY (z, x, y)
);
CREATE INDEX IF NOT EXISTS tiles_last_used ON tiles (last_used_at);
PRAGMA user_version = 1;
"""


class TileStore:
    """One layer's SQLite file. Used from the event loop thread only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit
        if self.db.execute("PRAGMA page_count").fetchone()[0] == 0:
            # Only takes effect before the first table: lets evicted pages
            # be returned to the filesystem.
            self.db.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.executescript(_SCHEMA)

    def get(self, z: int, x: int, y: int) -> tuple[bytes, int, str | None] | None:
        row = self.db.execute(
            "SELECT data, made_at, source_version FROM tiles WHERE z=? AND x=? AND y=?",
            (z, x, y),
        ).fetchone()
        return (bytes(row[0]), row[1], row[2]) if row else None

    def put(self, z: int, x: int, y: int, data: bytes, version: str | None, now: int) -> None:
        self.db.execute(
            "INSERT INTO tiles (z, x, y, data, made_at, source_version, last_used_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (z, x, y) DO UPDATE SET data=excluded.data,"
            " made_at=excluded.made_at, source_version=excluded.source_version,"
            " last_used_at=excluded.last_used_at",
            (z, x, y, data, now, version, now),
        )

    def touch(self, z: int, x: int, y: int, now: int) -> None:
        self.db.execute("UPDATE tiles SET last_used_at=? WHERE z=? AND x=? AND y=?", (now, z, x, y))

    def used_bytes(self) -> int:
        """Bytes in use (pages not on the free list), WAL included."""
        page = self.db.execute("PRAGMA page_size").fetchone()[0]
        count = self.db.execute("PRAGMA page_count").fetchone()[0]
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        return (count - free) * page

    def oldest(self, limit: int) -> list[tuple[int, int, int]]:
        """(last_used_at, rowid, size) of the least recently used tiles."""
        return self.db.execute(
            "SELECT last_used_at, rowid, length(data) FROM tiles"
            " ORDER BY last_used_at, rowid LIMIT ?", (limit,),
        ).fetchall()

    def delete(self, rowids: list[int]) -> None:
        self.db.executemany("DELETE FROM tiles WHERE rowid=?", [(r,) for r in rowids])

    def compact(self) -> None:
        """Give freed pages back to the filesystem."""
        self.db.execute("PRAGMA incremental_vacuum").fetchall()  # runs one page per step
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def close(self) -> None:
        self.db.close()


def _read_mbtiles(path: Path, z: int, x: int, y: int) -> bytes:
    """Blocking: one tile as stored in an MBTiles file (TMS rows); b"" if absent."""
    if not path.is_file():
        raise PackTileError(f"{path.name} is missing; run make ski-tiles-refresh")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, (1 << z) - 1 - y),
        ).fetchone()
    finally:
        con.close()
    return bytes(row[0]) if row else b""


def read_mbtiles_metadata(path: Path) -> dict:
    """The parsed `json` row of an MBTiles file's metadata (vector_layers
    and so on); {} if it has none."""
    if not path.is_file():
        raise PackTileError(f"{path.name} is missing; run make ski-tiles-refresh")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM metadata WHERE name='json'").fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else {}


def _retry_after(r: httpx.Response) -> float:
    try:
        return min(max(float(r.headers.get("retry-after", 0)), 0.0), RETRY_AFTER_MAX_S)
    except ValueError:  # an HTTP date: wait the most
        return RETRY_AFTER_MAX_S


# ── the cache ──────────────────────────────────────────────────────────────────

class PackCache:
    usgs_attempts = 5
    version_ttl_s = 600.0  # MinIO ETags are looked up at most this often

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        max_bytes: int | None = None,
        *,
        usgs_contact: str | None = None,
        ski_dir: Path | str = SKI_DIR,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        settings = get_settings()
        self.dir = Path(cache_dir or settings.pack_cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = settings.pack_cache_max_gb * GB if max_bytes is None else max_bytes
        self.usgs_contact = settings.usgs_contact if usgs_contact is None else usgs_contact
        self.ski_dir = Path(ski_dir)
        self._http = http or httpx.AsyncClient(
            timeout=30.0, limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
        self._clock = clock
        self._stores = {p.name: TileStore(p) for p in sorted(self.dir.glob("*.sqlite"))}
        self._usgs = asyncio.Semaphore(USGS_CONCURRENCY)
        self._own = asyncio.Semaphore(OWN_CONCURRENCY)
        self._pool = ThreadPoolExecutor(max_workers=OWN_CONCURRENCY, thread_name_prefix="pack-tiles")
        self._in_flight: dict[tuple, asyncio.Future[bytes]] = {}
        self._etags: dict[str, tuple[str, float]] = {}
        self._written = 0
        # (layer, "made" | "cached") -> tiles, for the pack worker's log.
        self.stats: Counter[tuple[str, str]] = Counter()

    async def aclose(self) -> None:
        for store in self._stores.values():
            store.close()
        self._pool.shutdown(wait=True)
        await self._http.aclose()

    def _store(self, layer: Layer, region: str) -> TileStore:
        name = file_name(layer, region)
        if name not in self._stores:
            self._stores[name] = TileStore(self.dir / name)
        return self._stores[name]

    async def get_tile(self, layer: str, region: str, z: int, x: int, y: int) -> bytes:
        """The tile's bytes (b"" for a real empty answer). Raises
        PackTileError if it can't be made; nothing is stored then."""
        spec = LAYERS.get(layer)
        if spec is None:
            raise ValueError(f"unknown pack layer {layer!r}")
        validate_region(region)
        if not (spec.minzoom <= z <= spec.maxzoom and 0 <= x < 1 << z and 0 <= y < 1 << z):
            raise ValueError(f"{layer} has no tile {z}/{x}/{y}")

        store = self._store(spec, region)
        version = await self._source_version(spec, region, z)
        now = int(self._clock())
        row = store.get(z, x, y)
        if row is not None and self._fresh(spec, row, version, now):
            store.touch(z, x, y, now)
            self.stats[(layer, "cached")] += 1
            return row[0]

        # Two packs asking for the same tile at once share one make.
        key = (store.path.name, z, x, y)
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._make_and_store(spec, store, region, z, x, y, version))
            self._in_flight[key] = task
            task.add_done_callback(lambda _: self._in_flight.pop(key, None))
        return await asyncio.shield(task)

    @staticmethod
    def _fresh(spec: Layer, row: tuple[bytes, int, str | None], version: str | None, now: int) -> bool:
        data, made_at, stored_version = row
        if spec.kind == "usgs":
            return bool(data) or now - made_at < USGS_EMPTY_TTL_S
        return stored_version == version

    async def _make_and_store(
        self, spec: Layer, store: TileStore, region: str, z: int, x: int, y: int, version: str | None,
    ) -> bytes:
        if spec.kind == "usgs":
            data = await self._fetch_usgs(spec, z, x, y)
        elif spec.kind == "colormap":
            data = await self._make_colormap(spec, region, z, x, y)
        elif spec.kind == "elevation":
            data = await self._make_elevation(region, z, x, y)
        else:
            data = await self._make_ski(region, z, x, y)
        store.put(z, x, y, data, version, int(self._clock()))
        self.stats[(spec.name, "made")] += 1
        self._written += len(data)
        if self._written >= EVICT_EVERY_BYTES:
            self._written = 0
            self.evict()
        return data

    # ── USGS ──

    async def _fetch_usgs(self, spec: Layer, z: int, x: int, y: int) -> bytes:
        if not self.usgs_contact:
            raise PackTileError("USGS_CONTACT is not set; the USGS layers are off")
        url = USGS_URL.format(service=spec.usgs_service, z=z, x=x, y=y)
        headers = {"User-Agent": f"whumpf/0.1 (+https://whumpf.co; {self.usgs_contact})"}

        async def _do() -> httpx.Response:
            r = await self._http.get(url, headers=headers)
            if r.status_code == 404:  # no tile there: a real empty answer
                return r
            if r.status_code == 429:
                await asyncio.sleep(_retry_after(r))
            r.raise_for_status()
            return r

        async with self._usgs:
            try:
                r = await call_with_resilience(
                    "usgs", _do, attempts=self.usgs_attempts, retry_429=True)
            except CircuitOpenError as exc:
                raise PackTileError("USGS circuit open") from exc
            except httpx.HTTPError as exc:
                raise PackTileError(f"USGS {spec.name} {z}/{x}/{y}: {exc!r}") from exc
        if r.status_code == 404:
            return b""
        if not r.headers.get("content-type", "").startswith("image/") or not r.content:
            raise PackTileError(
                f"USGS {spec.name} {z}/{x}/{y}: not an image ({r.headers.get('content-type')!r})")
        return r.content

    # ── our layers ──

    async def _make_colormap(self, spec: Layer, region: str, z: int, x: int, y: int) -> bytes:
        async with self._own:
            try:
                r = await terrain_tiles.fetch_colormap(
                    self._http, get_settings(), spec.name, region, z, x, y)  # type: ignore[arg-type]
            except (CircuitOpenError, httpx.HTTPError) as exc:
                raise PackTileError(f"TiTiler {spec.name} {z}/{x}/{y}: {exc!r}") from exc
            if r.status_code == 404:  # outside the COG
                return b""
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, terrain_tiles.to_palette_png, r.content)

    async def _make_elevation(self, region: str, z: int, x: int, y: int) -> bytes:
        dem_url, fallback_url = terrain_tiles.terrain_urls(get_settings(), region, z)
        bounds = terrain_tiles.tile_mercator_bounds(z, x, y)
        async with self._own:
            loop = asyncio.get_running_loop()
            try:
                png = await loop.run_in_executor(
                    self._pool, terrain_tiles.render_terrain_rgb_with_fallback,
                    dem_url, fallback_url, *bounds, 9,
                )
            except Exception as exc:
                raise PackTileError(f"elevation {z}/{x}/{y}: {exc!r}") from exc
        # No data: the same flat 0 m tile the live endpoint serves.
        return png or terrain_tiles.FLAT_TERRAIN_PNG

    def _ski_path(self, region: str) -> Path:
        return self.ski_dir / f"ski_{region}.mbtiles"

    async def _make_ski(self, region: str, z: int, x: int, y: int) -> bytes:
        async with self._own:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, _read_mbtiles, self._ski_path(region), z, x, y)

    # ── source versions ──

    async def source_version(self, layer: str, region: str) -> str | None:
        """The layer's version as a whole (None for USGS layers): what its
        top zooms are made from, which for elevation is both DEMs."""
        spec = LAYERS[layer]
        return await self._source_version(spec, region, spec.maxzoom)

    async def _source_version(self, spec: Layer, region: str, z: int) -> str | None:
        if spec.kind == "usgs":
            return None
        if spec.kind == "ski":
            # The weekly publish (and a rollback) swaps the file in, so its
            # mtime changes whenever the served tiles do.
            try:
                return str(self._ski_path(region).stat().st_mtime_ns)
            except FileNotFoundError as exc:
                raise PackTileError(f"ski_{region}.mbtiles is missing") from exc
        if spec.kind == "colormap":
            keys = [f"{region}/{terrain_tiles.colormap_cog(spec.name)}"]  # type: ignore[arg-type]
        elif z >= 13:  # hires with the 10m fallback: both matter
            keys = [f"{region}/dem_hires.tif", f"{region}/dem.tif"]
        else:
            keys = [f"{region}/dem.tif"]
        return "|".join([await self._etag(key) for key in keys])

    async def _etag(self, key: str) -> str:
        """The MinIO ETag of a dem-cogs object ("missing" if it isn't there)."""
        hit = self._etags.get(key)
        if hit and time.monotonic() - hit[1] < self.version_ttl_s:
            return hit[0]
        settings = get_settings()
        url = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{key}"

        async def _do() -> httpx.Response:
            r = await self._http.head(url)
            if r.status_code != 404:
                r.raise_for_status()
            return r

        try:
            r = await call_with_resilience("minio", _do)
        except (CircuitOpenError, httpx.HTTPError) as exc:
            raise PackTileError(f"can't read the version of {key}: {exc!r}") from exc
        etag = "missing" if r.status_code == 404 else r.headers.get("etag", "").strip('"')
        self._etags[key] = (etag, time.monotonic())
        return etag

    # ── size cap ──

    def used_bytes(self) -> int:
        return sum(store.used_bytes() for store in self._stores.values())

    def evict(self) -> int:
        """Above the cap, delete the least recently used tiles across all
        layers until under EVICT_TO of it. Returns the number deleted."""
        total = self.used_bytes()
        if total <= self.max_bytes:
            return 0
        target = self.max_bytes * EVICT_TO
        removed = 0
        while total > target:
            batch = sorted(
                (used, name, rowid, size)
                for name, store in self._stores.items()
                for used, rowid, size in store.oldest(1000)
            )
            if not batch:
                break
            doomed: dict[str, list[int]] = defaultdict(list)
            freed = 0
            for _, name, rowid, size in batch:
                doomed[name].append(rowid)
                freed += size
                if total - freed <= target:
                    break
            for name, rowids in doomed.items():
                self._stores[name].delete(rowids)
                removed += len(rowids)
            total = self.used_bytes()
        for store in self._stores.values():
            store.compact()
        logger.info("pack cache over %d bytes: evicted %d tiles, %d bytes now in use",
                    self.max_bytes, removed, self.used_bytes())
        return removed
