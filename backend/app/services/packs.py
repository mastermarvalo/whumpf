"""Offline packs (docs/runbook.md §12): the box, the tiles, the PMTiles files
and their upload to R2. The pack worker (app/jobs/pack_worker.py) runs
build_pack for one queued pack at a time; B3's endpoints will queue packs
through queue_pack.

- The box is rounded out to the zoom 14 grid. Every layer covers that, at
  its own zooms (pack_cache.LAYERS); elevation is padded by two zoom 14
  tiles on every side, for the runout check on the phone.
- Each layer is one PMTiles v3 file: tiles as made (JPEG, PNG, gzipped MVT),
  gzipped directories, identical tiles stored once, empty answers left out.
  A layer with no tiles at all in the box gets no file.
- Files go to R2 at packs/<pack id>/<layer>.pmtiles, sent with Content-MD5
  and checked against R2's ETag and size afterwards.
- A failed build is marked failed with a reason and its uploads are deleted.
  Nothing here logs the R2 credentials, endpoint or a botocore message.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import math
import re
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from geoalchemy2.shape import from_shape, to_shape
from pmtiles.tile import Compression, Entry, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from shapely.geometry import box
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Pack
from app.regions import REGIONS, validate_region
from app.services.pack_cache import (
    LAYERS,
    PackCache,
    PackTileError,
    read_mbtiles_metadata,
)

logger = logging.getLogger("whumpf.packs")

GRID_ZOOM = 14
ELEVATION_PAD = 2       # zoom 14 tiles on every side
MAX_SIDE_M = 25_000     # a pack's box is at most this on each side
MAX_ATTEMPTS = 3        # builds started (crashes included) before a pack fails
WINDOW = 32             # tiles asked for ahead of the one being written
PROGRESS_EVERY_S = 1.0

TILE_SIZE = {"topo": 256, "imagery": 256, "slope": 512, "aspect": 512, "elevation": 256, "ski": 512}
ATTRIBUTION = {
    "topo": "USGS The National Map",
    "imagery": "USGS The National Map: Orthoimagery (NAIP)",
    "slope": "USGS 3DEP",
    "aspect": "USGS 3DEP",
    "elevation": "USGS 3DEP",
    "ski": "© OpenStreetMap contributors",
}
_TILE_TYPE = {"jpg": TileType.JPEG, "png": TileType.PNG, "mvt": TileType.MVT}

_MAX_LAT = 85.0511287798
_EPS = 1e-9


class PackBuildError(Exception):
    """A pack can't be built. The message is safe to store and log."""


# ── the box and its tiles ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TileRange:
    """Tiles x0..x1, y0..y1 (inclusive) at zoom z."""
    z: int
    x0: int
    y0: int
    x1: int
    y1: int

    def at(self, z: int) -> TileRange:
        """The same area at another zoom: children above, parents below."""
        if z >= self.z:
            d = z - self.z
            return TileRange(z, self.x0 << d, self.y0 << d, ((self.x1 + 1) << d) - 1, ((self.y1 + 1) << d) - 1)
        d = self.z - z
        return TileRange(z, self.x0 >> d, self.y0 >> d, self.x1 >> d, self.y1 >> d)

    def pad(self, n: int) -> TileRange:
        last = (1 << self.z) - 1
        return TileRange(self.z, max(self.x0 - n, 0), max(self.y0 - n, 0),
                         min(self.x1 + n, last), min(self.y1 + n, last))

    def tiles(self) -> Iterator[tuple[int, int, int]]:
        for x in range(self.x0, self.x1 + 1):
            for y in range(self.y0, self.y1 + 1):
                yield self.z, x, y

    def count(self) -> int:
        return (self.x1 - self.x0 + 1) * (self.y1 - self.y0 + 1)

    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in degrees."""
        n = 1 << self.z

        def lon(x: int) -> float:
            return x / n * 360.0 - 180.0

        def lat(y: int) -> float:
            return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))

        return lon(self.x0), lat(self.y1 + 1), lon(self.x1 + 1), lat(self.y0)


def _frac(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 1 << z
    lat = max(-_MAX_LAT, min(_MAX_LAT, lat))
    return (lon + 180.0) / 360.0 * n, (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n


def grid_range(bbox: tuple[float, float, float, float], z: int = GRID_ZOOM) -> TileRange:
    """The tiles at zoom z that cover bbox (west, south, east, north). An
    edge exactly on a tile edge doesn't pull in the next row."""
    west, south, east, north = bbox
    fx0, fy0 = _frac(west, north, z)
    fx1, fy1 = _frac(east, south, z)
    last = (1 << z) - 1
    x0, y0 = math.floor(fx0 + _EPS), math.floor(fy0 + _EPS)
    x1, y1 = max(x0, math.ceil(fx1 - _EPS) - 1), max(y0, math.ceil(fy1 - _EPS) - 1)
    return TileRange(z, min(max(x0, 0), last), min(max(y0, 0), last),
                     min(max(x1, 0), last), min(max(y1, 0), last))


def coverage(layer: str, base: TileRange) -> TileRange:
    """What a layer covers at zoom 14: the rounded box, padded for elevation."""
    return base.pad(ELEVATION_PAD) if layer == "elevation" else base


def layer_tiles(layer: str, base: TileRange) -> list[tuple[int, int, int]]:
    """Every tile of a layer, in PMTiles tile ID order."""
    spec = LAYERS[layer]
    area = coverage(layer, base)
    tiles = [t for z in range(spec.minzoom, spec.maxzoom + 1) for t in area.at(z).tiles()]
    return sorted(tiles, key=lambda t: zxy_to_tileid(*t))


def side_lengths_m(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """(width, height) of bbox in metres, near enough for a size limit."""
    west, south, east, north = bbox
    mid = math.radians((south + north) / 2)
    return (east - west) * 111_320.0 * math.cos(mid), (north - south) * 110_574.0


def check_request(region: str, bbox: Sequence[float], layers: Sequence[str] | None) -> tuple[
        tuple[float, float, float, float], list[str]]:
    """Checks a pack request; returns the box and the layers in LAYERS
    order. Raises ValueError (or validate_region's HTTPException)."""
    validate_region(region)
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise ValueError("bbox must be four numbers: west, south, east, north")
    west, south, east, north = (float(v) for v in bbox)
    if not (west < east and south < north):
        raise ValueError("bbox must have west < east and south < north")
    rw, rs, re_, rn = REGIONS[region].bbox
    if west < rw or east > re_ or south < rs or north > rn:
        raise ValueError(f"bbox must be inside the {region} region")
    width, height = side_lengths_m((west, south, east, north))
    if max(width, height) > MAX_SIDE_M:
        raise ValueError(f"bbox is {width / 1000:.1f} x {height / 1000:.1f} km; "
                         f"the most is {MAX_SIDE_M // 1000} km on a side")
    wanted = list(LAYERS) if layers is None else list(layers)
    unknown = sorted(set(wanted) - set(LAYERS))
    if unknown or not wanted:
        raise ValueError(f"layers must be some of {', '.join(LAYERS)}" +
                         (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    return (west, south, east, north), [name for name in LAYERS if name in wanted]


def queue_pack(
    session: Session, region: str, bbox: Sequence[float], layers: Sequence[str] | None = None,
    requested_by: int | None = None,
) -> Pack:
    """Adds a queued pack (committed). Raises ValueError for a bad request."""
    box_, names = check_request(region, bbox, layers)
    pack = Pack(region=region, bbox=from_shape(box(*box_), srid=4326), layers=names,
                requested_by=requested_by)
    session.add(pack)
    session.commit()
    return pack


def pack_bbox(pack: Pack) -> tuple[float, float, float, float]:
    return to_shape(pack.bbox).bounds  # type: ignore[return-value]


# ── the queue ──────────────────────────────────────────────────────────────────

def claim_next(session: Session) -> Pack | None:
    """The oldest queued pack, now building (committed), or None."""
    pack = session.scalars(
        select(Pack).where(Pack.status == "queued").order_by(Pack.created_at, Pack.id)
        .limit(1).with_for_update(skip_locked=True)
    ).first()
    if pack is None:
        session.rollback()
        return None
    pack.status = "building"
    pack.attempts += 1
    pack.tiles_done = 0
    pack.error = None
    session.commit()
    return pack


def requeue_interrupted(session: Session) -> tuple[int, int]:
    """Packs left building by a crash go back to the queue, or fail after
    MAX_ATTEMPTS starts. Only safe while no build runs (the worker's lock).
    Returns (requeued, failed)."""
    requeued = failed = 0
    for pack in session.scalars(select(Pack).where(Pack.status == "building")).all():
        if pack.attempts >= MAX_ATTEMPTS:
            pack.status = "failed"
            pack.error = f"interrupted {pack.attempts} times; not tried again"
            failed += 1
        else:
            pack.status = "queued"
            requeued += 1
        pack.tiles_done = 0
    session.commit()
    return requeued, failed


# ── PMTiles ────────────────────────────────────────────────────────────────────

class _Writer(Writer):
    """pmtiles' Writer finds identical tiles by Python's hash(), which can
    collide (a tile would silently get another tile's bytes). This one keys
    them by SHA-256. Tiles must come in tile ID order."""

    def write_tile(self, tileid: int, data: bytes) -> None:
        last = self.tile_entries[-1] if self.tile_entries else None
        if last is not None and tileid < last.tile_id + last.run_length:
            raise ValueError("tiles must be written in tile ID order")
        digest = hashlib.sha256(data).digest()
        found = self.hash_to_offset.get(digest)
        if found is None:
            self.tile_f.write(data)
            self.tile_entries.append(Entry(tileid, self.offset, len(data), 1))
            self.hash_to_offset[digest] = self.offset
            self.offset += len(data)
        elif last is not None and tileid == last.tile_id + last.run_length and last.offset == found:
            last.run_length += 1
        else:
            self.tile_entries.append(Entry(tileid, found, len(data), 1))
        self.addressed_tiles += 1


def _e7(degrees: float) -> int:
    return round(degrees * 10_000_000)


def pmtiles_header(layer: str, bounds: tuple[float, float, float, float],
                   center: tuple[float, float]) -> dict[str, Any]:
    spec = LAYERS[layer]
    west, south, east, north = bounds
    return {
        "tile_type": _TILE_TYPE[spec.tile_type],
        # Tiles are stored as made: ski MVT comes gzipped from its MBTiles.
        "tile_compression": Compression.GZIP if spec.tile_type == "mvt" else Compression.NONE,
        "min_lon_e7": _e7(west), "min_lat_e7": _e7(south),
        "max_lon_e7": _e7(east), "max_lat_e7": _e7(north),
        "center_zoom": min(max(12, spec.minzoom), spec.maxzoom),
        "center_lon_e7": _e7(center[0]), "center_lat_e7": _e7(center[1]),
    }


async def _in_order(
    cache: PackCache, layer: str, region: str, tiles: Sequence[tuple[int, int, int]],
) -> AsyncIterator[tuple[tuple[int, int, int], bytes]]:
    """Each tile's bytes in the order given, with up to WINDOW asked ahead
    (the cache's own limits decide how many are made at once)."""
    pending: deque[tuple[tuple[int, int, int], asyncio.Future[bytes]]] = deque()
    try:
        for zxy in tiles:
            pending.append((zxy, asyncio.ensure_future(cache.get_tile(layer, region, *zxy))))
            if len(pending) >= WINDOW:
                head, task = pending.popleft()
                yield head, await task
        while pending:
            head, task = pending.popleft()
            yield head, await task
    finally:
        for _, task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)


async def write_layer(
    cache: PackCache, layer: str, region: str, tiles: Sequence[tuple[int, int, int]],
    out: BinaryIO, header: dict[str, Any], metadata: dict[str, Any],
    on_tile: Callable[[], None] = lambda: None,
) -> int:
    """Writes one layer's PMTiles file to out. Returns the tiles written; 0
    means every answer was empty and nothing was written."""
    writer = _Writer(out)
    written = 0
    try:
        async with aclosing(_in_order(cache, layer, region, tiles)) as stream:
            async for zxy, data in stream:
                if data:
                    writer.write_tile(zxy_to_tileid(*zxy), data)
                    written += 1
                on_tile()
        if written:
            writer.finalize(header, metadata)
    finally:
        writer.tile_f.close()
    return written


def layer_metadata(cache: PackCache, pack: Pack, layer: str, version: str | None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": f"whumpf {layer}",
        "layer": layer,
        "region": pack.region,
        "pack": str(pack.id),
        "version": version,
        "attribution": ATTRIBUTION[layer],
        "tile_size": TILE_SIZE[layer],
    }
    if layer == "elevation":
        meta["encoding"] = "terrarium"
    if layer == "ski":
        meta["vector_layers"] = read_mbtiles_metadata(cache.ski_dir / f"ski_{pack.region}.mbtiles").get(
            "vector_layers", [])
    return meta


def file_md5(path: Path) -> tuple[str, int]:
    """(MD5 hex, bytes) of a file."""
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            md5.update(chunk)
            size += len(chunk)
    return md5.hexdigest(), size


# ── R2 ─────────────────────────────────────────────────────────────────────────

class PackStore(Protocol):
    def upload(self, path: Path, key: str, md5_hex: str, size: int) -> None: ...
    def delete(self, keys: list[str]) -> None: ...


_R2_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")


def check_r2_settings() -> None:
    """Raises PackBuildError if the R2_* settings can't work. Never puts a
    value in the message."""
    s = get_settings()
    if not (s.r2_account_id and s.r2_access_key_id and s.r2_secret_access_key and s.r2_bucket_packs):
        raise PackBuildError("R2 is not configured (R2_* settings)")
    if not _R2_ACCOUNT_ID.fullmatch(s.r2_account_id):
        raise PackBuildError(
            "R2_ACCOUNT_ID is the 32-character account ID, not the endpoint URL"
            if "://" in s.r2_account_id or "." in s.r2_account_id
            else "R2_ACCOUNT_ID must be the 32-character account ID")


class R2Store:
    """The packs bucket in Cloudflare R2 (R2_* settings)."""

    content_type = "application/vnd.pmtiles"

    def __init__(self, client: Any = None) -> None:
        check_r2_settings()
        s = get_settings()
        self.bucket = s.r2_bucket_packs
        self._s3 = client or boto3.client(
            "s3",
            endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=s.r2_access_key_id,
            aws_secret_access_key=s.r2_secret_access_key,
            region_name="auto",
            # R2 doesn't take boto3's default CRC checksums; Content-MD5 does
            # the same job here.
            config=Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def upload(self, path: Path, key: str, md5_hex: str, size: int) -> None:
        with path.open("rb") as f:
            self._s3.put_object(
                Bucket=self.bucket, Key=key, Body=f, ContentLength=size,
                ContentType=self.content_type,
                ContentMD5=base64.b64encode(bytes.fromhex(md5_hex)).decode(),
            )
        head = self._s3.head_object(Bucket=self.bucket, Key=key)
        if head.get("ContentLength") != size or head.get("ETag", "").strip('"') != md5_hex:
            raise PackBuildError(f"the R2 copy of {key} doesn't match the file")

    def delete(self, keys: list[str]) -> None:
        if keys:
            self._s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})


def object_key(pack: Pack, layer: str) -> str:
    return f"packs/{pack.id}/{layer}.pmtiles"


def reason(exc: BaseException) -> str:
    """A failure reason safe to store and log: botocore messages can carry
    the R2 endpoint, so only their code or type is kept."""
    if isinstance(exc, (PackBuildError, PackTileError)):
        return str(exc)[:500]
    if isinstance(exc, ClientError):
        return f"R2 error {exc.response.get('Error', {}).get('Code', 'unknown')}"
    if isinstance(exc, BotoCoreError):
        return f"R2 error {type(exc).__name__}"
    return f"unexpected error {type(exc).__name__}"


# ── a build ────────────────────────────────────────────────────────────────────

async def build_pack(
    session: Session, pack: Pack, cache: PackCache, store_factory: Callable[[], PackStore],
    build_root: Path, clock: Callable[[], float] = time.monotonic,
) -> None:
    """Builds a claimed pack and marks it ready or failed (committed). Never
    raises, except CancelledError on shutdown: then the pack goes back to the
    queue without using up an attempt. The build folder is removed either way."""
    work = build_root / str(pack.id)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    uploaded: list[str] = []
    store: PackStore | None = None
    pack_id, started = pack.id, clock()
    try:
        store = store_factory()
        region, bbox, names = pack.region, pack_bbox(pack), list(pack.layers)
        base = grid_range(bbox)
        center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        plan = {name: layer_tiles(name, base) for name in names}
        pack.tiles_total = sum(len(tiles) for tiles in plan.values())
        pack.tiles_done = 0
        session.commit()

        last_commit = clock()

        def on_tile() -> None:
            nonlocal last_commit
            pack.tiles_done += 1
            if clock() - last_commit >= PROGRESS_EVERY_S:
                session.commit()
                last_commit = clock()

        files: list[dict[str, Any]] = []
        versions: dict[str, str | None] = {}
        for name in names:
            tiles = plan[name]
            header = pmtiles_header(name, coverage(name, base).bounds(), center)
            path = work / f"{name}.pmtiles"
            layer_start = clock()
            made0, cached0 = cache.stats[(name, "made")], cache.stats[(name, "cached")]
            done0 = pack.tiles_done
            for _ in range(2):
                version = await cache.source_version(name, region)
                with path.open("wb") as out:
                    written = await write_layer(
                        cache, name, region, tiles, out, header,
                        layer_metadata(cache, pack, name, version), on_tile)
                if await cache.source_version(name, region) == version:
                    break
                logger.info("pack %s %s: its source changed during the build; building it again",
                            pack_id, name)
                pack.tiles_done = done0
            else:
                raise PackBuildError(f"{name}: its source changed during the build, twice")

            versions[name] = version
            if written:
                md5, size = file_md5(path)
                key = object_key(pack, name)
                uploaded.append(key)  # before the upload, so a partial one is deleted too
                await asyncio.to_thread(store.upload, path, key, md5, size)
                files.append({"layer": name, "version": version, "bytes": size, "md5": md5, "key": key})
            else:
                size = 0
                files.append({"layer": name, "version": version, "bytes": 0, "md5": None, "key": None})
            path.unlink(missing_ok=True)
            logger.info(
                "pack %s %s: %d tiles (%d made, %d from the cache), %d in the file, %d bytes, %.1f s",
                pack_id, name, len(tiles), cache.stats[(name, "made")] - made0,
                cache.stats[(name, "cached")] - cached0, written, size, clock() - layer_start)

        pack.files = files
        pack.source_versions = versions
        pack.status = "ready"
        pack.built_at = datetime.now(UTC)
        pack.error = None
        session.commit()
        logger.info("pack %s ready: %d bytes in %d files, %.1f s", pack_id,
                    sum(f["bytes"] for f in files), len([f for f in files if f["key"]]),
                    clock() - started)
    except asyncio.CancelledError:
        session.rollback()
        pack.status = "queued"
        pack.attempts = max(pack.attempts - 1, 0)
        pack.tiles_done = 0
        session.commit()
        _delete_uploads(store, uploaded, pack_id)
        logger.info("pack %s: stopped by shutdown, queued again", pack_id)
        raise
    except Exception as exc:
        why = reason(exc)
        if isinstance(exc, (PackBuildError, PackTileError, BotoCoreError, ClientError)):
            logger.warning("pack %s failed: %s", pack_id, why)
        else:
            logger.exception("pack %s failed: %s", pack_id, why)
        session.rollback()
        pack.status = "failed"
        pack.error = why
        session.commit()
        _delete_uploads(store, uploaded, pack_id)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _delete_uploads(store: PackStore | None, keys: list[str], pack_id: object) -> None:
    if store is None or not keys:
        return
    try:
        store.delete(keys)
    except Exception as exc:  # noqa: BLE001 — the build has failed already; keep its reason
        logger.warning("pack %s: couldn't delete its uploads: %s", pack_id, reason(exc))
