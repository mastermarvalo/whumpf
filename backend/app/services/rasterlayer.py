"""Gridded NOAA layers served from MinIO through TiTiler: shared pieces.

Recent snowfall, snow depth, current temperature and radar all work the same
way. A cron job fetches a NOAA grid, validates it, clips it to each region and
writes a COG plus a small JSON pointer into the dem-cogs bucket:

    dem-cogs/<layer>/<region>/.../<revision>.tif   one per revision, never rewritten
    dem-cogs/<layer>/<region>/latest.json          what is served

Every revision gets a new key, so TiTiler never reads a half-written file and
tile URLs carry the revision instead of needing a Cloudflare purge. The API
reads latest.json (routers/tiles.py, routers/weather_tiles.py) to find the
COG. docs/runbook.md §8 (snowfall) and §9 (weather).

Colour scales are lists of (min, max, hex). One list feeds both the legend
the apps show and TiTiler's interval colormap, so the two cannot drift apart.
A min of -1000 or less is an open lower end, a max of 1000 or more an open
upper end.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import boto3
import httpx
import numpy as np
from botocore.config import Config
from botocore.exceptions import ClientError
from rasterio.io import MemoryFile
from rasterio.windows import Window

from app.config import get_settings
from app.http_retry import call_with_resilience

logger = logging.getLogger("whumpf.rasterlayer")

Scale = list[tuple[float, float, str]]
_OPEN = 1000


class BadFile(Exception):
    """The download isn't a grid we're willing to serve."""


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── colour scales ─────────────────────────────────────────────────────────────

def _rgba(hex_color: str) -> list[int]:
    h = hex_color.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [255]


def titiler_colormap(scale: Scale) -> str:
    """TiTiler interval colormap: [[[min, max], [r, g, b, a]], ...], min inclusive."""
    return json.dumps([[[lo, hi], _rgba(color)] for lo, hi, color in scale], separators=(",", ":"))


def legend(scale: Scale) -> list[dict[str, Any]]:
    out = []
    for lo, hi, color in scale:
        if lo <= -_OPEN:
            label = f"< {hi:g}"
        elif hi >= _OPEN:
            label = f"{lo:g}+"
        else:
            label = f"{lo:g}–{hi:g}"
        out.append({
            "min": None if lo <= -_OPEN else lo,
            "max": None if hi >= _OPEN else hi,
            "color": color,
            "label": label,
        })
    return out


# ── latest.json, read by the API ──────────────────────────────────────────────
# Read over plain HTTP from the public dem-cogs bucket (as TiTiler does), and
# kept per worker for a minute: tiles hit this on every request. Each layer
# passes its own cache dict.

POINTER_TTL_S = 60.0

_HTTP = httpx.AsyncClient(timeout=5.0)


async def read_pointer(key: str, cache: dict[str, tuple[dict | None, float]]) -> dict | None:
    """The JSON at `key` in dem-cogs, or None if the job has not written it yet.

    Raises (httpx / CircuitOpenError / ValueError) if MinIO can't be read;
    callers answer 503 no-store.
    """
    cached = cache.get(key)
    if cached and time.monotonic() - cached[1] < POINTER_TTL_S:
        return cached[0]

    settings = get_settings()
    url = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{key}"

    async def _do() -> httpx.Response:
        r = await _HTTP.get(url)
        # MinIO answers 403 for a missing key when listing isn't public.
        if r.status_code not in (403, 404):
            r.raise_for_status()
        return r

    r = await call_with_resilience("minio", _do)
    pointer = None if r.status_code in (403, 404) else r.json()
    cache[key] = (pointer, time.monotonic())
    return pointer


def age_problem(label: str, valid: str | None, stale_after: timedelta, now: datetime) -> str | None:
    """A /status problem string if `valid` is missing or older than `stale_after`."""
    if not valid:
        return f"{label}: no data"
    age = now - datetime.fromisoformat(valid)
    if age > stale_after:
        if age >= timedelta(hours=2):
            return f"{label}: valid {valid} ({int(age.total_seconds() // 3600)} h old)"
        return f"{label}: valid {valid} ({int(age.total_seconds() // 60)} min old)"
    return None


# ── storage, used by the jobs ─────────────────────────────────────────────────

class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, body: bytes, content_type: str) -> None: ...
    def list(self, prefix: str) -> list[tuple[str, datetime]]: ...
    def delete(self, keys: list[str]) -> None: ...


class S3Store:
    """The dem-cogs bucket in MinIO."""

    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.s3_bucket_dem_cogs
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )

    def get(self, key: str) -> bytes | None:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType=content_type)

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        out = []
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix):
            out.extend((o["Key"], o["LastModified"]) for o in page.get("Contents", []))
        return out

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            self._s3.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True},
            )


class DryRunStore:
    """Reads from `inner`, logs what it would write or delete."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner

    def get(self, key: str) -> bytes | None:
        return self._inner.get(key)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        logger.info("dry run: would write %s (%d bytes)", key, len(body))

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        return self._inner.list(prefix)

    def delete(self, keys: list[str]) -> None:
        logger.info("dry run: would delete %s", keys)


def load_pointer(store: Store, key: str, default: dict) -> dict:
    raw = store.get(key)
    return json.loads(raw) if raw else default


def save_pointer(store: Store, key: str, pointer: dict, now: datetime) -> None:
    pointer["updated"] = iso(now)
    store.put(key, json.dumps(pointer, indent=1).encode(), "application/json")


def cleanup(store: Store, prefix: str, keep: set[str], older_than: datetime) -> int:
    """Delete .tif files under `prefix` that aren't in `keep` and were written
    before `older_than`. Returns how many went."""
    old = [
        key for key, modified in store.list(prefix)
        if key.endswith(".tif") and key not in keep and modified < older_than
    ]
    if old:
        store.delete(old)
    return len(old)


# ── downloads ─────────────────────────────────────────────────────────────────

async def download(
    client: httpx.AsyncClient, url: str, *, upstream: str, max_bytes: int,
    etag: str | None = None, byte_range: tuple[int, int] | None = None,
) -> tuple[int, bytes, dict]:
    """(status, body, headers). 404 and 304 are answers, not errors. Raises
    BadFile if the body grows past `max_bytes`. Retries and the circuit
    breaker are per `upstream`."""
    async def _do() -> tuple[int, bytes, dict]:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if byte_range:
            headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        async with client.stream("GET", url, headers=headers) as r:
            if r.status_code not in (304, 404):
                r.raise_for_status()
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise BadFile(f"{url} is over {max_bytes} bytes")
            return r.status_code, bytes(body), dict(r.headers)

    return await call_with_resilience(upstream, _do)


# ── rasters ───────────────────────────────────────────────────────────────────

def region_window(
    transform, width: int, height: int, bounds: tuple[float, float, float, float],
) -> Window:
    """The pixel window of a north-up lon/lat grid that covers `bounds`
    (west, south, east, north), snapped outwards to whole pixels. Raises
    BadFile if the region isn't inside the grid."""
    west, south, east, north = bounds
    inv = ~transform
    col0, row0 = inv * (west, north)
    col1, row1 = inv * (east, south)
    col0, row0 = math.floor(col0 + 1e-6), math.floor(row0 + 1e-6)
    col1, row1 = math.ceil(col1 - 1e-6), math.ceil(row1 - 1e-6)
    if col0 < 0 or row0 < 0 or col1 > width or row1 > height:
        raise BadFile("region is not inside the grid")
    return Window(col0, row0, col1 - col0, row1 - row0)


def write_cog(data: np.ndarray, transform, nodata: float, crs: str = "EPSG:4326") -> bytes:
    """A deflate-compressed float32 COG of one band."""
    out = data.astype(np.float32)
    with MemoryFile() as mem:
        with mem.open(
            driver="COG", width=out.shape[1], height=out.shape[0], count=1,
            dtype="float32", crs=crs, transform=transform, nodata=nodata,
            compress="deflate",
        ) as dst:
            dst.write(out, 1)
        return mem.read()
