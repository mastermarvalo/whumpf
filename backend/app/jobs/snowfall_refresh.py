"""Hourly job: NOHRSC 24 h / 72 h snowfall -> per-region COGs in MinIO.

    make snowfall-refresh          (hourly host cron, docs/runbook.md §8)
    make snowfall-refresh ARGS=--dry-run
    python -m app.jobs.snowfall_refresh [--region colorado] [--dry-run]

For each region and period, the newest 00Z/12Z issue on NOHRSC wins. The
served issue is re-checked every run with If-None-Match, because NOHRSC
reruns each analysis hourly for five days and overwrites the file in place.
A file that fails validation is never published; whatever was served stays.
Exits 1 if anything failed, so the cron log shows it.

Layout and pointer format: app/services/snowfall.py; the shared storage,
download and COG helpers are in app/services/rasterlayer.py.
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

from app.config import get_settings
from app.regions import REGIONS
from app.services import rasterlayer
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store
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


# ── NOHRSC ────────────────────────────────────────────────────────────────────

_iso = rasterlayer.iso


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
    return await rasterlayer.download(client, url, upstream="nohrsc", max_bytes=MAX_BYTES, etag=etag)


# ── validate + clip ───────────────────────────────────────────────────────────

def clip_to_cog(src_bytes: bytes, bounds: tuple[float, float, float, float]) -> tuple[bytes, dict]:
    """Clip a NOHRSC snowfall GeoTIFF to `bounds` (lon/lat) and return a COG
    plus stats. Raises BadFile if it doesn't look like the grid we expect.

    Nodata is set explicitly: the 48 h / 72 h files carry -99999 without a
    NoData tag.
    """
    try:
        with MemoryFile(src_bytes) as mem, mem.open() as src:
            if src.count != 1 or src.crs is None or src.crs.to_epsg() != 4326:
                raise BadFile(f"expected 1 band in EPSG:4326, got {src.count} in {src.crs}")
            if not all(math.isclose(r, RESOLUTION_DEG, abs_tol=1e-6) for r in src.res):
                raise BadFile(f"expected {RESOLUTION_DEG}° pixels, got {src.res}")
            window = rasterlayer.region_window(src.transform, src.width, src.height, bounds)
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

    out = np.where(valid, np.maximum(data, 0.0), NODATA)
    cog = rasterlayer.write_cog(out, transform, NODATA)
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
    if n := rasterlayer.cleanup(store, f"{prefix(region)}/", keep, now - KEEP):
        logger.info("%s: deleted %d old revisions", region, n)


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    now: datetime | None = None, base: str = NOHRSC_BASE,
) -> bool:
    """Refresh every region and period. Returns True if all went well."""
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        pointer = rasterlayer.load_pointer(store, pointer_key(region), {"region": region, "periods": {}})
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
            rasterlayer.save_pointer(store, pointer_key(region), pointer, now)
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
