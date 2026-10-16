"""Hourly job: SNODAS snow depth -> per-region COGs in MinIO.

    make snowdepth-refresh          (hourly host cron, docs/runbook.md §9)
    make snowdepth-refresh ARGS=--dry-run
    python -m app.jobs.snowdepth_refresh [--region colorado] [--dry-run]

SNODAS is a daily analysis valid at 06Z. NSIDC posts each day's archive
(a ~8 MB tar of gzipped flat binaries) around midday. Each run asks for days
newer than the served one, newest first, and re-checks the served day with
If-None-Match, so most runs are one 304 or 404. A file that fails validation
is never published; whatever was served stays. Exits 1 if anything failed.

Layout and pointer format: app/services/snowdepth.py.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import logging
import math
import re
import sys
import tarfile
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
from rasterio.transform import from_origin
from rasterio.windows import transform as window_transform

from app.config import get_settings
from app.regions import REGIONS
from app.services import rasterlayer
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store
from app.services.snowdepth import NODATA, cog_key, pointer_key, prefix

logger = logging.getLogger("whumpf.snowdepth_refresh")

NSIDC_BASE = "https://noaadata.apps.nsidc.org/NOAA/G02158/masked"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
VALID_HOUR = 6
LOOKBACK = timedelta(days=3)
# The masked CONUS tar is ~2–10 MB.
MAX_BYTES = 40 * 1024 * 1024
# The snow depth grid is 6935 × 3351 int16 (~46 MB unpacked).
MAX_GRID_BYTES = 64 * 1024 * 1024
RESOLUTION_DEG = 1 / 120
MAX_INCHES = 1000.0
MIN_VALID_SHARE = 0.5
KEEP = timedelta(days=7)
MM_PER_INCH = 25.4

# Product 1036 is snow depth; the others in the archive are SWE, melt, etc.
_DEPTH = re.compile(r"^us_ssmv11036tS__T0001TTNATS(\d{8})05HP001\.(txt|dat)\.gz$")


def day_url(base: str, day: datetime) -> str:
    return f"{base}/{day:%Y}/{day:%m}_{_MONTHS[day.month - 1]}/SNODAS_{day:%Y%m%d}.tar"


def candidate_days(now: datetime) -> list[datetime]:
    """06Z valid times from `now` back to LOOKBACK, newest first."""
    t = now.astimezone(UTC).replace(hour=VALID_HOUR, minute=0, second=0, microsecond=0)
    if t > now:
        t -= timedelta(days=1)
    out = []
    while t >= now - LOOKBACK:
        out.append(t)
        t -= timedelta(days=1)
    return out


# ── unpack + validate + clip ──────────────────────────────────────────────────

def _gunzip(raw: bytes, limit: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
        out = f.read(limit + 1)
    if len(out) > limit:
        raise BadFile(f"unpacks to over {limit} bytes")
    return out


def _header(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def read_depth(tar_bytes: bytes) -> tuple[dict[str, str], bytes]:
    """The snow depth header and grid from a SNODAS daily archive."""
    found: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            for member in tar:
                m = _DEPTH.match(member.name)
                if m and member.isfile():
                    f = tar.extractfile(member)
                    if f is not None:
                        found[m.group(2)] = f.read(MAX_BYTES + 1)
    except tarfile.TarError as exc:
        raise BadFile(f"not a readable tar: {exc}") from exc
    if set(found) != {"txt", "dat"}:
        raise BadFile("no snow depth grid in the archive")
    try:
        header = _header(_gunzip(found["txt"], 64 * 1024).decode("ascii", "replace"))
        grid = _gunzip(found["dat"], MAX_GRID_BYTES)
    except (OSError, EOFError) as exc:
        raise BadFile(f"bad gzip member: {exc}") from exc
    return header, grid


def clip_to_cog(
    header: dict[str, str], grid: bytes, bounds: tuple[float, float, float, float],
) -> tuple[bytes, dict, datetime]:
    """Clip a SNODAS snow depth grid to `bounds` and return a COG in inches,
    stats and the valid time. Raises BadFile if it isn't the grid we expect."""
    try:
        units = header["Data units"]
        cols, rows = int(header["Number of columns"]), int(header["Number of rows"])
        west = float(header["Minimum x-axis coordinate"])
        north = float(header["Maximum y-axis coordinate"])
        xres, yres = float(header["X-axis resolution"]), float(header["Y-axis resolution"])
        nodata = float(header["No data value"])
        bpp = int(header["Data bytes per pixel"])
        valid = datetime(
            int(header["Start year"]), int(header["Start month"]), int(header["Start day"]),
            int(header["Start hour"]), tzinfo=UTC,
        )
    except (KeyError, ValueError) as exc:
        raise BadFile(f"header is missing or has a bad {exc}") from exc
    if not units.startswith("Meters / 1000") or bpp != 2:
        raise BadFile(f"expected int16 millimetres, got {bpp} bytes of {units!r}")
    if not all(math.isclose(r, RESOLUTION_DEG, rel_tol=1e-4) for r in (xres, yres)):
        raise BadFile(f"expected 1/120° pixels, got {xres}, {yres}")
    if len(grid) != cols * rows * 2:
        raise BadFile(f"grid is {len(grid)} bytes, header says {cols} × {rows}")

    transform = from_origin(west, north, xres, yres)
    window = rasterlayer.region_window(transform, cols, rows, bounds)
    full = np.frombuffer(grid, dtype=">i2").reshape(rows, cols)
    r0, c0 = int(window.row_off), int(window.col_off)
    data = full[r0:r0 + int(window.height), c0:c0 + int(window.width)].astype(np.float32)

    ok = data != nodata
    share = float(ok.mean()) if data.size else 0.0
    if share < MIN_VALID_SHARE:
        raise BadFile(f"only {share:.0%} of the region has data")
    inches = data / MM_PER_INCH
    values = inches[ok]
    lo, hi = float(values.min()), float(values.max())
    if lo < 0 or hi > MAX_INCHES:
        raise BadFile(f"values {lo:.1f}..{hi:.1f} in are out of range")

    out = np.where(ok, inches, NODATA)
    cog = rasterlayer.write_cog(out, window_transform(window, transform), NODATA)
    return cog, {
        "max_in": round(hi, 1),
        "snow_share": round(float((values >= 1).mean()), 3),
    }, valid


# ── one region ────────────────────────────────────────────────────────────────

async def refresh_region(
    client: httpx.AsyncClient, store: Store, region: str,
    entry: dict | None, now: datetime, base: str = NSIDC_BASE,
) -> tuple[dict | None, bool]:
    """Return (new pointer entry or None if unchanged, failed)."""
    bounds = REGIONS[region].max_bounds
    served = datetime.fromisoformat(entry["valid"]) if entry else None
    failed = False

    for day in candidate_days(now):
        if served and day < served:
            break
        url = day_url(base, day)
        etag = entry.get("etag") if entry and day == served else None
        status, body, headers = await rasterlayer.download(
            client, url, upstream="nsidc", max_bytes=MAX_BYTES, etag=etag,
        )
        if status == 404:
            continue
        if status == 304:
            logger.info("%s: %s unchanged", region, f"{day:%Y%m%d}")
            return None, failed

        revision = f"{day:%Y%m%d}-{hashlib.sha256(body).hexdigest()[:8]}"
        if entry and revision == entry["revision"]:
            return {**entry, "etag": headers.get("etag")}, failed
        try:
            header, grid = read_depth(body)
            cog, stats, valid = clip_to_cog(header, grid, bounds)
            if valid != day:
                raise BadFile(f"archive for {day:%Y-%m-%d} is valid {valid}")
        except BadFile as exc:
            logger.error("%s: rejected %s: %s", region, url, exc)
            failed = True
            continue

        key = cog_key(region, revision)
        store.put(key, cog, "image/tiff")
        logger.info("%s: published %s (max %.0f in)", region, revision, stats["max_in"])
        return {
            "valid": rasterlayer.iso(valid),
            "revision": revision,
            "key": key,
            "etag": headers.get("etag"),
            "fetched": rasterlayer.iso(now),
            **stats,
        }, failed

    if entry is None:
        logger.error("%s: no SNODAS day found in the last %s", region, LOOKBACK)
        failed = True
    return None, failed


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    now: datetime | None = None, base: str = NSIDC_BASE,
) -> bool:
    """Refresh every region. Returns True if all went well."""
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        pointer = rasterlayer.load_pointer(store, pointer_key(region), {"region": region})
        try:
            new, failed = await refresh_region(client, store, region, pointer.get("current"), now, base)
        except Exception:
            logger.exception("%s: refresh failed", region)
            new, failed = None, True
        ok = ok and not failed
        if new is not None:
            pointer["current"] = new
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
