"""Hourly job: RTMA 2 m temperature -> per-region COGs in MinIO.

    make temperature-refresh          (hourly host cron, docs/runbook.md §9)
    make temperature-refresh ARGS=--dry-run
    python -m app.jobs.temperature_refresh [--region colorado] [--dry-run]

RTMA (Real-Time Mesoscale Analysis) is an hourly 2.5 km analysis on the NDFD
Lambert grid. Each analysis file is ~100 MB, so the job reads its .idx and
downloads only the 2 m temperature message (~6 MB) with a Range request.
NOAA's AWS bucket is tried first, then NOMADS. Hours newer than the served
one are tried newest first; the analyses are not rewritten, so the served
hour is never fetched again. A file that fails validation is never
published. Exits 1 if anything failed.

Layout and pointer format: app/services/temperature.py.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import math
import sys
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

from app.config import get_settings
from app.http_retry import CircuitOpenError
from app.regions import REGIONS
from app.services import rasterlayer
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store
from app.services.temperature import NODATA, cog_key, pointer_key, prefix

logger = logging.getLogger("whumpf.temperature_refresh")

# (upstream name for retries/breaker, base URL)
SOURCES = (
    ("rtma-aws", "https://noaa-rtma-pds.s3.amazonaws.com"),
    ("rtma-nomads", "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rtma/prod"),
)
LOOKBACK = timedelta(hours=6)
MAX_IDX_BYTES = 64 * 1024
# The temperature message is ~6 MB.
MAX_BYTES = 20 * 1024 * 1024
GRID_RES_M = 2539.703
OUT_RES_DEG = 0.025
MIN_C, MAX_C = -60.0, 60.0
MIN_VALID_SHARE = 0.5
KEEP = timedelta(days=7)
_FIELD = ("TMP", "2 m above ground")


def file_url(base: str, hour: datetime) -> str:
    return f"{base}/rtma2p5.{hour:%Y%m%d}/rtma2p5.t{hour:%H}z.2dvaranl_ndfd.grb2_wexp"


def candidate_hours(now: datetime) -> list[datetime]:
    """Whole hours from `now` back to LOOKBACK, newest first."""
    t = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out = []
    while t >= now - LOOKBACK:
        out.append(t)
        t -= timedelta(hours=1)
    return out


def message_range(idx: str, hour: datetime) -> tuple[int, int]:
    """Byte range of the 2 m temperature message from a wgrib2 .idx:
    `n:offset:d=YYYYMMDDHH:VAR:LEVEL:anl:` per message."""
    rows = [line.split(":") for line in idx.splitlines() if line.strip()]
    for i, row in enumerate(rows):
        if len(row) >= 5 and (row[3], row[4]) == _FIELD:
            if row[2] != f"d={hour:%Y%m%d%H}":
                raise BadFile(f"index is for {row[2]}, not {hour:%Y%m%d%H}")
            if i + 1 >= len(rows):
                raise BadFile("temperature is the last message; no end offset")
            try:
                return int(row[1]), int(rows[i + 1][1]) - 1
            except ValueError as exc:
                raise BadFile(f"bad offset in index: {exc}") from exc
    raise BadFile("no 2 m temperature in the index")


# ── validate + reproject ──────────────────────────────────────────────────────

def _grib_time(value: str | None) -> int | None:
    # GDAL writes "1792116000" (or "1792116000 sec UTC" in older versions).
    try:
        return int((value or "").split()[0])
    except (IndexError, ValueError):
        return None


def to_cog(
    message: bytes, hour: datetime, bounds: tuple[float, float, float, float],
) -> tuple[bytes, dict]:
    """Reproject one RTMA 2 m temperature message to a lon/lat COG in °F
    covering `bounds`. Raises BadFile if it isn't the field we expect."""
    west, south, east, north = bounds
    width = math.ceil((east - west) / OUT_RES_DEG - 1e-6)
    height = math.ceil((north - south) / OUT_RES_DEG - 1e-6)
    dst_transform = from_origin(west, north, OUT_RES_DEG, OUT_RES_DEG)
    out = np.full((height, width), NODATA, dtype=np.float32)
    try:
        with MemoryFile(message) as mem, mem.open() as src:
            tags = src.tags(1)
            if src.count != 1 or tags.get("GRIB_ELEMENT") != "TMP" or tags.get("GRIB_UNIT") != "[C]":
                raise BadFile(f"expected one TMP band in [C], got {src.count} bands, {tags.get('GRIB_ELEMENT')}")
            if _grib_time(tags.get("GRIB_VALID_TIME")) != int(hour.timestamp()):
                raise BadFile(f"valid time {tags.get('GRIB_VALID_TIME')} is not {hour:%Y-%m-%dT%HZ}")
            if src.crs is None or not src.crs.is_projected:
                raise BadFile(f"expected the NDFD Lambert grid, got {src.crs}")
            if not all(math.isclose(r, GRID_RES_M, rel_tol=1e-3) for r in src.res):
                raise BadFile(f"expected 2.5 km pixels, got {src.res}")
            reproject(
                source=rasterio.band(src, 1), destination=out,
                src_nodata=src.nodata, dst_transform=dst_transform, dst_crs="EPSG:4326",
                dst_nodata=NODATA, resampling=Resampling.bilinear,
            )
    except rasterio.errors.RasterioIOError as exc:
        raise BadFile(f"not a readable GRIB2 message: {exc}") from exc

    valid = out != NODATA
    share = float(valid.mean())
    if share < MIN_VALID_SHARE:
        raise BadFile(f"only {share:.0%} of the region has data")
    lo, hi = float(out[valid].min()), float(out[valid].max())
    if lo < MIN_C or hi > MAX_C:
        raise BadFile(f"values {lo:.1f}..{hi:.1f} °C are out of range")

    fahrenheit = np.where(valid, out * 9 / 5 + 32, NODATA)
    cog = rasterlayer.write_cog(fahrenheit, dst_transform, NODATA)
    return cog, {"min_f": round(lo * 9 / 5 + 32), "max_f": round(hi * 9 / 5 + 32)}


# ── one hour from one source ──────────────────────────────────────────────────

async def fetch_message(
    client: httpx.AsyncClient, upstream: str, base: str, hour: datetime,
) -> bytes | None:
    """The 2 m temperature message for `hour`, or None if `base` doesn't
    have that hour yet."""
    url = file_url(base, hour)
    status, idx, _ = await rasterlayer.download(
        client, f"{url}.idx", upstream=upstream, max_bytes=MAX_IDX_BYTES,
    )
    if status == 404:
        return None
    lo, hi = message_range(idx.decode("ascii", "replace"), hour)
    status, body, _ = await rasterlayer.download(
        client, url, upstream=upstream, max_bytes=MAX_BYTES, byte_range=(lo, hi),
    )
    if status == 404:
        return None
    if len(body) != hi - lo + 1:
        raise BadFile(f"asked for {hi - lo + 1} bytes, got {len(body)}")
    return body


async def refresh(
    client: httpx.AsyncClient, store: Store, regions: list[str],
    pointers: dict[str, dict], now: datetime, sources=SOURCES,
) -> tuple[dict[str, dict], bool]:
    """Publish the newest hour newer than what each region serves. Returns
    ({region: new entry}, failed). One download serves every region."""
    served = [
        datetime.fromisoformat(p["current"]["valid"]) if "current" in p else None
        for p in pointers.values()
    ]
    oldest_served = None if None in served else min(served, default=None)
    # A rejected file always fails the run; an unreachable source only
    # if nothing new could be published (the other source may have it).
    failed = unreachable = False

    for hour in candidate_hours(now):
        if oldest_served and hour <= oldest_served:
            logger.info("%s unchanged", f"{oldest_served:%Y%m%d%H}")
            return {}, failed or unreachable
        for upstream, base in sources:
            try:
                message = await fetch_message(client, upstream, base, hour)
            except BadFile as exc:
                logger.error("%s %s: rejected: %s", upstream, f"{hour:%Y%m%d%H}", exc)
                failed = True
                continue
            except (httpx.HTTPError, CircuitOpenError) as exc:
                logger.warning("%s %s: %r", upstream, f"{hour:%Y%m%d%H}", exc)
                unreachable = True
                continue
            if message is None:
                continue

            revision = f"{hour:%Y%m%d%H}-{hashlib.sha256(message).hexdigest()[:8]}"
            new: dict[str, dict] = {}
            try:
                for region in regions:
                    cog, stats = to_cog(message, hour, REGIONS[region].max_bounds)
                    key = cog_key(region, revision)
                    store.put(key, cog, "image/tiff")
                    logger.info("%s: published %s (%d..%d °F)", region, revision, stats["min_f"], stats["max_f"])
                    new[region] = {
                        "valid": rasterlayer.iso(hour),
                        "revision": revision,
                        "key": key,
                        "source": upstream,
                        "fetched": rasterlayer.iso(now),
                        **stats,
                    }
            except BadFile as exc:
                logger.error("%s %s: rejected: %s", upstream, f"{hour:%Y%m%d%H}", exc)
                failed = True
                continue
            return new, failed

    if oldest_served is None:
        logger.error("no RTMA hour found in the last %s", LOOKBACK)
        failed = True
    return {}, failed or unreachable


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    now: datetime | None = None, sources=SOURCES,
) -> bool:
    """Refresh every region. Returns True if all went well."""
    now = now or datetime.now(UTC)
    pointers = {
        region: rasterlayer.load_pointer(store, pointer_key(region), {"region": region})
        for region in regions
    }
    try:
        new, failed = await refresh(client, store, regions, pointers, now, sources)
    except Exception:
        logger.exception("refresh failed")
        new, failed = {}, True
    ok = not failed
    for region, pointer in pointers.items():
        if region in new:
            pointer["current"] = new[region]
            rasterlayer.save_pointer(store, pointer_key(region), pointer, now)
        try:
            keep = {pointer["current"]["key"]} if "current" in pointer else set()
            if n := rasterlayer.cleanup(store, f"{prefix(region)}/", keep, now - KEEP):
                logger.info("%s: deleted %d old revisions", region, n)
        except Exception:
            logger.exception("%s: cleanup failed", region)
            ok = False
    return ok


async def _main(regions: list[str], dry_run: bool) -> bool:
    headers = {"User-Agent": get_settings().weather_user_agent}
    store: Store = DryRunStore(S3Store()) if dry_run else S3Store()
    async with httpx.AsyncClient(headers=headers, timeout=120.0, follow_redirects=True) as client:
        return await run(store, client, regions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", choices=sorted(REGIONS), action="append",
                        help="default: every region")
    parser.add_argument("--dry-run", action="store_true",
                        help="download and validate, but write nothing to MinIO")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(0 if asyncio.run(_main(args.region or sorted(REGIONS), args.dry_run)) else 1)


if __name__ == "__main__":
    main()
