"""Offline packs (docs/runbook.md §12): the box, the tiles, the PMTiles files
and their upload to R2. The pack worker (app/jobs/pack_worker.py) runs
build_pack for one queued pack at a time. POST /packs (app/routers/packs.py)
asks for packs through request_pack, which reuses a pack built for the same
request; make pack-queue adds one through queue_pack.

- The box is rounded out to the zoom 14 grid. Every layer covers that, at
  its own zooms (pack_cache.LAYERS); elevation is padded by two zoom 14
  tiles on every side, for the runout check on the phone.
- Each layer is one PMTiles v3 file: tiles as made (JPEG, PNG, gzipped MVT),
  gzipped directories, identical tiles stored once, empty answers left out.
  A layer with no tiles at all in the box gets no file.
- Files go to R2 at packs/<pack id>/<layer>.pmtiles, sent with Content-MD5
  and checked against R2's ETag and size afterwards.
- A failed build is marked failed with a reason and its uploads are deleted.
  Nothing here logs the R2 credentials, endpoint, a botocore message or a
  download link.
- R2 deletes files 30 days after upload (a lifecycle rule on the bucket);
  a ready pack older than 29 days, or with a file gone, is queued again.
  cleanup() forgets packs nobody has asked for in 60 days.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import re
import shutil
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from geoalchemy2.shape import from_shape, to_shape
from pmtiles.tile import Compression, Entry, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from shapely.geometry import box
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Pack, PackRequest
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
MAX_AREA_KM2 = 700      # a pack's box is at most this big
MAX_SIDE_M = 50_000     # and at most this on each side
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

REQUIRED_LAYERS = ("topo", "slope", "aspect", "elevation", "ski")
OPTIONAL_LAYERS = ("imagery",)

# Requests (docs/api-packs.md). The hourly and polling limits are the packs
# and packs_poll buckets in tile_tokens.BUCKETS.
MAX_ACTIVE_PER_USER = 2   # a user's packs queued or building at once
MAX_ACTIVE = 20           # packs queued or building on the server
ACTIVE_RETRY_AFTER_S = 60
QUEUE_FULL_RETRY_AFTER_S = 300
UNAVAILABLE_RETRY_AFTER_S = 60
URL_TTL_S = 24 * 3600
# R2's lifecycle rule deletes files 30 days after upload, up to a day late;
# a pack older than this is built again rather than handed out.
READY_FOR = timedelta(days=29)
FORGET_AFTER = timedelta(days=60)
FORGET_FAILED_AFTER = timedelta(days=1)
FAILED_MESSAGE = "This pack couldn't be built. Try again later."
# Transaction-level advisory lock around queueing and cleanup, so two
# requests can't both pass the caps or create the same pack.
REQUEST_LOCK_KEY = 0x7061636C  # "pacl"

# Average bytes per tile asked for, per zoom, from the Berthoud Pass build
# (Oct 2026; empty tiles count 0), and the tiles it had per zoom: the
# starting point for GET /packs/manifest, pooled with later builds.
SEED_TILE_BYTES: dict[str, dict[int, tuple[int, int]]] = {
    layer: {8 + i: (n, avg) for i, (n, avg) in enumerate(zip(counts, avgs, strict=True))}
    for layer, counts, avgs in (
        ("topo", (1, 1, 2, 4, 9, 16, 42, 168, 672),
         (36112, 36562, 33281, 32625, 28427, 25476, 20855, 19662, 13086)),
        ("imagery", (1, 1, 2, 4, 9, 16, 42, 168, 672),
         (23313, 34912, 31706, 22701, 20759, 19534, 22507, 23627, 24420)),
        ("slope", (1, 1, 2, 4, 9, 16, 42),
         (226833, 229073, 179967, 180969, 173784, 166183, 66930)),
        ("aspect", (1, 1, 2, 4, 9, 16, 42),
         (263335, 262466, 208633, 200384, 187843, 165162, 65163)),
        ("elevation", (1, 1, 2, 4, 9, 36, 110),
         (158352, 155676, 149604, 143496, 135133, 124780, 112535)),
        ("ski", (1, 1, 2, 4, 9, 16, 42), (1166, 788, 1065, 3324, 1105, 688, 174)),
    )
}

_MAX_LAT = 85.0511287798
_EPS = 1e-9


class PackBuildError(Exception):
    """A pack can't be built. The message is safe to store and log."""


class PackRefused(Exception):
    """A pack request turned down: the HTTP status, a message for the user
    and Retry-After seconds."""

    def __init__(self, status: int, detail: str, retry_after: int) -> None:
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


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


def tiles_total(base: TileRange, layers: Sequence[str]) -> int:
    """The tiles of every layer of a pack (what build_pack will count)."""
    return sum(coverage(name, base).at(z).count()
               for name in layers for z in range(LAYERS[name].minzoom, LAYERS[name].maxzoom + 1))


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
    if width * height / 1e6 > MAX_AREA_KM2:
        raise ValueError(f"bbox is {width / 1000:.1f} x {height / 1000:.1f} km "
                         f"({width * height / 1e6:.0f} km²); the most is {MAX_AREA_KM2} km²")
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


def check_api_layers(layers: Sequence[str]) -> list[str]:
    """POST /packs: the required layers, maybe imagery; in LAYERS order."""
    wanted = set(layers)
    if not set(REQUIRED_LAYERS) <= wanted or not wanted <= set(REQUIRED_LAYERS + OPTIONAL_LAYERS):
        raise ValueError("layers must include topo, slope, aspect, elevation and ski; imagery is optional")
    return [name for name in LAYERS if name in wanted]


def _id_key() -> bytes:
    return hmac.new(get_settings().jwt_secret.encode(), b"whumpf-pack-id-v1", hashlib.sha256).digest()


def pack_id(region: str, base: TileRange, layers: Sequence[str],
            versions: dict[str, str | None]) -> uuid.UUID:
    """The same request (region, rounded box, layers, each layer's source
    version) gives the same id. Keyed from JWT_SECRET, so ids (and the R2
    paths) can't be worked out from a box."""
    body = json.dumps({"region": region, "tiles": [base.z, base.x0, base.y0, base.x1, base.y1],
                       "layers": list(layers), "versions": {n: versions.get(n) for n in layers}},
                      sort_keys=True, separators=(",", ":"))
    return uuid.UUID(bytes=hmac.new(_id_key(), body.encode(), hashlib.sha256).digest()[:16])


def is_fresh(pack: Pack, now: datetime) -> bool:
    """Ready and young enough that its files are in R2 for a whole URL_TTL_S."""
    return pack.status == "ready" and pack.built_at is not None and now - pack.built_at < READY_FOR


def queue_position(session: Session, pack: Pack) -> int | None:
    """1 for the next pack the worker takes; None unless queued."""
    if pack.status != "queued":
        return None
    ahead = session.scalar(select(func.count()).select_from(Pack).where(
        Pack.status == "queued",
        (Pack.queued_at < pack.queued_at) | ((Pack.queued_at == pack.queued_at) & (Pack.id < pack.id)),
    ))
    return (ahead or 0) + 1


def _lock(session: Session) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": REQUEST_LOCK_KEY})


def _check_caps(session: Session, pack_id_: uuid.UUID, user_id: int, new_build: bool) -> None:
    mine = session.scalar(
        select(func.count()).select_from(PackRequest).join(Pack, Pack.id == PackRequest.pack_id)
        .where(PackRequest.user_id == user_id, PackRequest.pack_id != pack_id_,
               Pack.status.in_(("queued", "building"))))
    if (mine or 0) >= MAX_ACTIVE_PER_USER:
        raise PackRefused(429, f"You already have {MAX_ACTIVE_PER_USER} packs being prepared. "
                               "Try again when one is ready.", ACTIVE_RETRY_AFTER_S)
    if new_build:
        active = session.scalar(select(func.count()).select_from(Pack).where(
            Pack.status.in_(("queued", "building"))))
        if (active or 0) >= MAX_ACTIVE:
            raise PackRefused(503, "Too many packs are being prepared right now. "
                                   "Try again in a few minutes.", QUEUE_FULL_RETRY_AFTER_S)


def _requeue(pack: Pack, now: datetime) -> None:
    pack.status = "queued"
    pack.attempts = 0
    pack.tiles_done = 0
    pack.files = []
    pack.source_versions = {}
    pack.error = None
    pack.built_at = None
    pack.queued_at = now


def take_request(
    session: Session, user_id: int, pid: uuid.UUID, *, present: bool | None,
    new: Callable[[], Pack] | None = None,
) -> tuple[Pack, bool]:
    """Records that user_id asked for pack pid and queues it if it needs a
    build: new (made by `new`), failed, or ready but not `present` in R2.
    Joining a queued or building pack counts toward the user's active
    packs; a present ready pack is never refused. Raises PackRefused, or
    LookupError if there is no such pack and no `new`. Returns (pack, queued
    by this call); committed."""
    now = datetime.now(UTC)
    _lock(session)
    pack = session.scalars(select(Pack).where(Pack.id == pid).execution_options(
        populate_existing=True)).first()
    if pack is None and new is None:
        session.rollback()
        raise LookupError(pid)
    try:
        if pack is not None and pack.status == "ready" and present and is_fresh(pack, now):
            queued = False
        elif pack is not None and pack.status in ("queued", "building"):
            _check_caps(session, pid, user_id, new_build=False)
            queued = False
        else:
            _check_caps(session, pid, user_id, new_build=True)
            if pack is None:
                assert new is not None
                pack = new()
                pack.queued_at = now
                session.add(pack)
            else:
                _requeue(pack, now)
            queued = True
    except PackRefused:
        session.rollback()
        raise
    pack.last_requested_at = now
    session.flush()
    session.execute(insert(PackRequest).values(pack_id=pid, user_id=user_id, requested_at=now)
                    .on_conflict_do_update(index_elements=["pack_id", "user_id"],
                                           set_={"requested_at": now}))
    session.commit()
    return pack, queued


def new_api_pack(pid: uuid.UUID, region: str, base: TileRange, layers: Sequence[str],
                 user_id: int) -> Pack:
    """A pack row for POST /packs: the box stored rounded, so no user's
    exact box is shown to another."""
    return Pack(id=pid, region=region, bbox=from_shape(box(*base.bounds()), srid=4326),
                layers=list(layers), requested_by=user_id, tiles_total=tiles_total(base, layers))


def cleanup(session: Session, now: datetime | None = None) -> int:
    """Forgets ready and failed packs nobody has asked for in FORGET_AFTER
    (failed ones after FORGET_FAILED_AFTER). Their R2 files are gone by
    then (the 30-day lifecycle rule). Returns the packs deleted; committed."""
    now = now or datetime.now(UTC)
    _lock(session)
    ids = session.scalars(select(Pack.id).where(
        Pack.status.in_(("ready", "failed")),
        (Pack.last_requested_at < now - FORGET_AFTER)
        | ((Pack.status == "failed") & (Pack.last_requested_at < now - FORGET_FAILED_AFTER)),
    )).all()
    if ids:
        session.execute(delete(PackRequest).where(PackRequest.pack_id.in_(ids)))
        session.flush()
        session.execute(delete(Pack).where(Pack.id.in_(ids)))
    session.commit()
    return len(ids)


def tile_byte_averages(session: Session, region: str, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """{layer: {zoom: average bytes per tile asked for}}: the Berthoud seed
    (as one pack) pooled with ready packs built in the last FORGET_AFTER."""
    now = now or datetime.now(UTC)
    totals = {layer: {z: [n, n * avg] for z, (n, avg) in zooms.items()}
              for layer, zooms in SEED_TILE_BYTES.items()}
    for stats in session.scalars(select(Pack.tile_bytes).where(
            Pack.region == region, Pack.status == "ready", Pack.built_at > now - FORGET_AFTER)):
        for layer, zooms in (stats or {}).items():
            for z, (n, size) in zooms.items():
                slot = totals.setdefault(layer, {}).setdefault(int(z), [0, 0])
                slot[0] += n
                slot[1] += size
    return {layer: {str(z): round(size / n) for z, (n, size) in sorted(zooms.items()) if n}
            for layer, zooms in totals.items()}


# ── the queue ──────────────────────────────────────────────────────────────────

def claim_next(session: Session) -> Pack | None:
    """The oldest queued pack, now building (committed), or None."""
    pack = session.scalars(
        select(Pack).where(Pack.status == "queued").order_by(Pack.queued_at, Pack.id)
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
    on_tile: Callable[[int, int], None] = lambda z, size: None,
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
                on_tile(zxy[0], len(data))
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

    def exists(self, key: str) -> bool:
        """False if R2 has no such object; other errors raise."""
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def presign(self, key: str, ttl_s: int = URL_TTL_S) -> str:
        """A GET link valid ttl_s. Signed locally; never log it."""
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl_s)


def files_present(store: R2Store, pack: Pack) -> bool:
    """Every file of a ready pack is still in R2 (blocking)."""
    return all(store.exists(f["key"]) for f in pack.files if f.get("key"))


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

        sizes: dict[str, dict[str, list[int]]] = {}
        layer_sizes: dict[str, list[int]] = {}

        def on_tile(z: int, size: int) -> None:
            nonlocal last_commit
            slot = layer_sizes.setdefault(str(z), [0, 0])
            slot[0] += 1
            slot[1] += size
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
                layer_sizes = {}
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
            sizes[name] = layer_sizes
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
        pack.tile_bytes = sizes
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
