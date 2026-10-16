"""Hourly job: NOHRSC 24 h / 72 h snowfall -> per-region COGs in MinIO.

    make snowfall-refresh          (hourly host cron, docs/runbook.md §8)
    make snowfall-refresh ARGS=--dry-run
    python -m app.jobs.snowfall_refresh [--region colorado] [--dry-run]

For each region and period, the newest 00Z/12Z issue on NOHRSC wins. The
served issue is re-checked every run with If-None-Match, because NOHRSC
reruns each analysis hourly for five days and overwrites the file in place.
A file that fails validation is never published; whatever was served stays.
Exits 1 if anything failed, so the cron log shows it.

Layout and pointer format: app/services/snowfall.py.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import sys
from datetime import UTC, datetime, timedelta
from typing import Protocol

import boto3
import httpx
import numpy as np
import rasterio
from botocore.config import Config
from botocore.exceptions import ClientError
from rasterio.io import MemoryFile
from rasterio.windows import Window

from app.config import get_settings
from app.http_retry import call_with_resilience
from app.regions import REGIONS
from app.services.snowfall import NODATA, PERIODS, cog_key, pointer_key, prefix

logger = logging.getLogger("whumpf.snowfall_refresh")

NOHRSC_BASE = "https://www.nohrsc.noaa.gov/snowfall/data"
ISSUE_HOURS = (0, 12)
# How far back to look for an issue when the newest ones aren't there yet.
LOOKBACK = timedelta(days=3)
# Source files are ~0.5 MB; anything near this is not a NOHRSC grid.
MAX_BYTES = 25 * 1024 * 1024
RESOLUTION_DEG = 0.04
MAX_INCHES = 200.0
# Share of the region window that must be real data (CONUS land).
MIN_VALID_SHARE = 0.5
# Revisions no longer referenced by latest.json are deleted after this.
KEEP = timedelta(days=7)


class BadFile(Exception):
    """The download isn't a NOHRSC snowfall grid we're willing to serve."""


# ── storage ───────────────────────────────────────────────────────────────────

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


# ── NOHRSC ────────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_times(now: datetime) -> list[datetime]:
    """00Z and 12Z issues from `now` back to LOOKBACK, newest first."""
    t = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out = []
    while t >= now - LOOKBACK:
        if t.hour in ISSUE_HOURS:
            out.append(t)
        t -= timedelta(hours=1)
    return out


def file_url(base: str, period: str, valid: datetime) -> str:
    return f"{base}/{valid:%Y%m}/sfav2_CONUS_{period}_{valid:%Y%m%d%H}.tif"


async def _download(client: httpx.AsyncClient, url: str, etag: str | None) -> tuple[int, bytes, dict]:
    """(status, body, headers). 404 and 304 are answers, not errors."""
    async def _do() -> tuple[int, bytes, dict]:
        headers = {"If-None-Match": etag} if etag else {}
        async with client.stream("GET", url, headers=headers) as r:
            if r.status_code not in (304, 404):
                r.raise_for_status()
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise BadFile(f"{url} is over {MAX_BYTES} bytes")
            return r.status_code, bytes(body), dict(r.headers)

    return await call_with_resilience("nohrsc", _do)


# ── validate + clip ───────────────────────────────────────────────────────────

def clip_to_cog(src_bytes: bytes, bounds: tuple[float, float, float, float]) -> tuple[bytes, dict]:
    """Clip a NOHRSC snowfall GeoTIFF to `bounds` (lon/lat) and return a COG
    plus stats. Raises BadFile if it doesn't look like the grid we expect.

    Nodata is set explicitly: the 48 h / 72 h files carry -99999 without a
    NoData tag.
    """
    west, south, east, north = bounds
    try:
        with MemoryFile(src_bytes) as mem, mem.open() as src:
            if src.count != 1 or src.crs is None or src.crs.to_epsg() != 4326:
                raise BadFile(f"expected 1 band in EPSG:4326, got {src.count} in {src.crs}")
            if not all(math.isclose(r, RESOLUTION_DEG, abs_tol=1e-6) for r in src.res):
                raise BadFile(f"expected {RESOLUTION_DEG}° pixels, got {src.res}")
            inv = ~src.transform
            col0, row0 = inv * (west, north)
            col1, row1 = inv * (east, south)
            col0, row0 = math.floor(col0 + 1e-6), math.floor(row0 + 1e-6)
            col1, row1 = math.ceil(col1 - 1e-6), math.ceil(row1 - 1e-6)
            if col0 < 0 or row0 < 0 or col1 > src.width or row1 > src.height:
                raise BadFile("region is not inside the grid")
            window = Window(col0, row0, col1 - col0, row1 - row0)
            data = src.read(1, window=window).astype(np.float32)
            transform = src.window_transform(window)
    except rasterio.errors.RasterioIOError as exc:
        raise BadFile(f"not a readable GeoTIFF: {exc}") from exc

    valid = data > NODATA / 10  # anything near -99999 is nodata
    share = float(valid.mean()) if data.size else 0.0
    if share < MIN_VALID_SHARE:
        raise BadFile(f"only {share:.0%} of the region has data")
    values = data[valid]
    lo, hi = float(values.min()), float(values.max())
    if lo < -0.01 or hi > MAX_INCHES:
        raise BadFile(f"values {lo:.2f}..{hi:.2f} in are out of range")

    out = np.where(valid, np.maximum(data, 0.0), NODATA).astype(np.float32)
    with MemoryFile() as mem:
        with mem.open(
            driver="COG", width=out.shape[1], height=out.shape[0], count=1,
            dtype="float32", crs="EPSG:4326", transform=transform, nodata=NODATA,
            compress="deflate",
        ) as dst:
            dst.write(out, 1)
        cog = mem.read()
    return cog, {"max_in": round(hi, 1), "snow_share": round(float((values >= 0.1).mean()), 3)}


# ── one region / period ───────────────────────────────────────────────────────

async def refresh_period(
    client: httpx.AsyncClient, store: Store, region: str, period: str,
    entry: dict | None, now: datetime, base: str = NOHRSC_BASE,
) -> tuple[dict | None, bool]:
    """Return (new pointer entry or None if unchanged, failed)."""
    bounds = REGIONS[region].max_bounds
    served = datetime.fromisoformat(entry["valid"]) if entry else None
    failed = False

    for valid in issue_times(now):
        if served and valid < served:
            break
        url = file_url(base, period, valid)
        etag = entry.get("etag") if entry and valid == served else None
        status, body, headers = await _download(client, url, etag)
        if status == 404:
            continue
        if status == 304:
            logger.info("%s %s: %s unchanged", region, period, f"{valid:%Y%m%d%H}")
            return None, failed

        revision = f"{valid:%Y%m%d%H}-{hashlib.sha256(body).hexdigest()[:8]}"
        if entry and revision == entry["revision"]:
            # Rewritten with the same bytes: only remember the new ETag.
            logger.info("%s %s: %s same content, new ETag", region, period, revision)
            return {**entry, "etag": headers.get("etag")}, failed
        try:
            cog, stats = clip_to_cog(body, bounds)
        except BadFile as exc:
            # Keep looking: an older issue (or the served one) may be fine.
            logger.error("%s %s: rejected %s: %s", region, period, url, exc)
            failed = True
            continue

        key = cog_key(region, period, revision)
        store.put(key, cog, "image/tiff")
        logger.info("%s %s: published %s (max %.1f in)", region, period, revision, stats["max_in"])
        return {
            "valid": _iso(valid),
            "start": _iso(valid - PERIODS[period]),
            "revision": revision,
            "key": key,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "fetched": _iso(now),
            **stats,
        }, failed

    if entry is None:
        logger.error("%s %s: no issue found in the last %s", region, period, LOOKBACK)
        failed = True
    return None, failed


def cleanup(store: Store, region: str, pointer: dict, now: datetime) -> None:
    keep = {e["key"] for e in pointer.get("periods", {}).values()}
    old = [
        key for key, modified in store.list(f"{prefix(region)}/")
        if key.endswith(".tif") and key not in keep and modified < now - KEEP
    ]
    if old:
        store.delete(old)
        logger.info("%s: deleted %d old revisions", region, len(old))


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    now: datetime | None = None, base: str = NOHRSC_BASE,
) -> bool:
    """Refresh every region and period. Returns True if all went well."""
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        raw = store.get(pointer_key(region))
        pointer = json.loads(raw) if raw else {"region": region, "periods": {}}
        changed = False
        for period in PERIODS:
            try:
                new, failed = await refresh_period(
                    client, store, region, period, pointer["periods"].get(period), now, base,
                )
            except Exception:
                logger.exception("%s %s: refresh failed", region, period)
                ok = False
                continue
            ok = ok and not failed
            if new is not None:
                pointer["periods"][period] = new
                changed = True
        if changed:
            pointer["updated"] = _iso(now)
            store.put(pointer_key(region), json.dumps(pointer, indent=1).encode(), "application/json")
        try:
            cleanup(store, region, pointer, now)
        except Exception:
            logger.exception("%s: cleanup failed", region)
            ok = False
    return ok


async def _main(regions: list[str], dry_run: bool) -> bool:
    headers = {"User-Agent": get_settings().nohrsc_user_agent}
    store: Store = DryRunStore(S3Store()) if dry_run else S3Store()
    async with httpx.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True) as client:
        return await run(store, client, regions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", choices=sorted(REGIONS), action="append",
                        help="default: every region")
    parser.add_argument("--dry-run", action="store_true",
                        help="download and validate, but write nothing to MinIO")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    sys.exit(0 if asyncio.run(_main(args.region or sorted(REGIONS), args.dry_run)) else 1)


if __name__ == "__main__":
    main()
