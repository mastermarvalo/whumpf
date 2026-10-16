"""Hourly job: RTMA 2 m temperature and 10 m wind -> per-region files in MinIO.

    make temperature-refresh          (hourly host cron, docs/runbook.md §9)
    make temperature-refresh ARGS=--dry-run
    python -m app.jobs.temperature_refresh [--region colorado] [--dry-run]

RTMA (Real-Time Mesoscale Analysis) is an hourly 2.5 km analysis on the NDFD
Lambert grid. Each analysis file is ~85 MB, so the job reads its .idx and
downloads only the messages it needs with Range requests: the 2 m
temperature (~6 MB) and the 10 m wind direction, speed and gust, which sit
next to each other in the file (~17 MB, one request). NOAA's AWS bucket is
tried first, then NOMADS.

Temperature becomes a COG per region (app/services/temperature.py), wind a
gzipped GeoJSON grid of arrows per region (app/services/wind.py). Each is
published on its own: hours newer than the one a layer serves are tried
newest first, and the analyses are not rewritten, so a served hour is never
fetched again. A file that fails validation is never published. Exits 1 if
anything failed.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
from app.services import rasterlayer, temperature, wind
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store
from app.services.temperature import NODATA

logger = logging.getLogger("whumpf.temperature_refresh")

# (upstream name for retries/breaker, base URL)
SOURCES = (
    ("rtma-aws", "https://noaa-rtma-pds.s3.amazonaws.com"),
    ("rtma-nomads", "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rtma/prod"),
)
LOOKBACK = timedelta(hours=6)
MAX_IDX_BYTES = 64 * 1024
# One request is the temperature (~6 MB) or the three wind messages (~17 MB).
MAX_BYTES = 40 * 1024 * 1024
GRID_RES_M = 2539.703
OUT_RES_DEG = 0.025
MIN_C, MAX_C = -60.0, 60.0
MAX_MPH = 200.0
MS_TO_MPH = 2.2369363
MIN_VALID_SHARE = 0.5
KEEP = timedelta(days=7)

Field = tuple[str, str]
_FIELD: Field = ("TMP", "2 m above ground")
WDIR: Field = ("WDIR", "10 m above ground")
WIND: Field = ("WIND", "10 m above ground")
GUST: Field = ("GUST", "10 m above ground")
_NAMES = {
    _FIELD: "2 m temperature",
    WDIR: "10 m wind direction",
    WIND: "10 m wind speed",
    GUST: "10 m gust",
}


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


def message_range(idx: str, hour: datetime, field: Field = _FIELD) -> tuple[int, int]:
    """Byte range of one message from a wgrib2 .idx:
    `n:offset:d=YYYYMMDDHH:VAR:LEVEL:anl:` per message."""
    name = _NAMES[field]
    rows = [line.split(":") for line in idx.splitlines() if line.strip()]
    for i, row in enumerate(rows):
        if len(row) >= 5 and (row[3], row[4]) == field:
            if row[2] != f"d={hour:%Y%m%d%H}":
                raise BadFile(f"index is for {row[2]}, not {hour:%Y%m%d%H}")
            if i + 1 >= len(rows):
                raise BadFile(f"{name} is the last message; no end offset")
            try:
                return int(row[1]), int(rows[i + 1][1]) - 1
            except ValueError as exc:
                raise BadFile(f"bad offset in index: {exc}") from exc
    raise BadFile(f"no {name} in the index")


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join byte ranges that touch, so neighbouring messages are one request."""
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


# ── validate ──────────────────────────────────────────────────────────────────

def _grib_time(value: str | None) -> int | None:
    # GDAL writes "1792116000" (or "1792116000 sec UTC" in older versions).
    try:
        return int((value or "").split()[0])
    except (IndexError, ValueError):
        return None


def _check_band(src, element: str, unit: str, hour: datetime) -> None:
    """Raise BadFile unless `src` is one RTMA band of `element` in `unit`
    for `hour` on the 2.5 km NDFD Lambert grid."""
    tags = src.tags(1)
    if src.count != 1 or tags.get("GRIB_ELEMENT") != element or tags.get("GRIB_UNIT") != unit:
        raise BadFile(
            f"expected one {element} band in {unit}, got {src.count} bands, "
            f"{tags.get('GRIB_ELEMENT')} in {tags.get('GRIB_UNIT')}"
        )
    if _grib_time(tags.get("GRIB_VALID_TIME")) != int(hour.timestamp()):
        raise BadFile(f"valid time {tags.get('GRIB_VALID_TIME')} is not {hour:%Y-%m-%dT%HZ}")
    if src.crs is None or not src.crs.is_projected:
        raise BadFile(f"expected the NDFD Lambert grid, got {src.crs}")
    if not all(math.isclose(r, GRID_RES_M, rel_tol=1e-3) for r in src.res):
        raise BadFile(f"expected 2.5 km pixels, got {src.res}")


def _out_grid(bounds: tuple[float, float, float, float]):
    """(width, height, transform) of the region's OUT_RES_DEG lon/lat grid."""
    west, south, east, north = bounds
    width = math.ceil((east - west) / OUT_RES_DEG - 1e-6)
    height = math.ceil((north - south) / OUT_RES_DEG - 1e-6)
    return width, height, from_origin(west, north, OUT_RES_DEG, OUT_RES_DEG)


# ── temperature ───────────────────────────────────────────────────────────────

def to_cog(
    message: bytes, hour: datetime, bounds: tuple[float, float, float, float],
) -> tuple[bytes, dict]:
    """Reproject one RTMA 2 m temperature message to a lon/lat COG in °F
    covering `bounds`. Raises BadFile if it isn't the field we expect."""
    width, height, dst_transform = _out_grid(bounds)
    out = np.full((height, width), NODATA, dtype=np.float32)
    try:
        with MemoryFile(message) as mem, mem.open() as src:
            _check_band(src, "TMP", "[C]", hour)
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


# ── wind ──────────────────────────────────────────────────────────────────────

def _read_wind_band(message: bytes, element: str, unit: str, hour: datetime):
    """(values with NaN for nodata, transform, crs) of one message."""
    try:
        with MemoryFile(message) as mem, mem.open() as src:
            _check_band(src, element, unit, hour)
            values = src.read(1, masked=True).astype(np.float64).filled(np.nan)
            return values, src.transform, src.crs
    except rasterio.errors.RasterioIOError as exc:
        raise BadFile(f"not a readable GRIB2 message: {exc}") from exc


def _blocks(a: np.ndarray, n: int) -> np.ndarray:
    """(rows/n, cols/n, n*n): the n×n blocks of `a`, NaN-padded at the edges."""
    h, w = a.shape
    padded = np.full((-(-h // n) * n, -(-w // n) * n), np.nan)
    padded[:h, :w] = a
    rows, cols = padded.shape[0] // n, padded.shape[1] // n
    return padded.reshape(rows, n, cols, n).swapaxes(1, 2).reshape(rows, cols, n * n)


def _lvl(row: int, col: int) -> int:
    if row % 4 == 0 and col % 4 == 0:
        return 0
    if row % 2 == 0 and col % 2 == 0:
        return 1
    return 2


def to_arrows(
    wdir: bytes, speed: bytes, gust: bytes, hour: datetime,
    bounds: tuple[float, float, float, float],
) -> tuple[bytes, dict]:
    """The three RTMA 10 m wind messages as a gzipped GeoJSON point grid
    over `bounds` (see app/services/wind.py). Raises BadFile if they aren't
    the fields we expect.

    Direction is never interpolated (350° and 10° would average to 180°):
    it is turned into east/north components with the speed first, and the
    direction of each arrow comes from the mean of those."""
    d, d_tf, d_crs = _read_wind_band(wdir, "WDIR", "[deg true]", hour)
    s, s_tf, s_crs = _read_wind_band(speed, "WIND", "[m/s]", hour)
    g, g_tf, g_crs = _read_wind_band(gust, "GUST", "[m/s]", hour)
    if not (d.shape == s.shape == g.shape and d_tf == s_tf == g_tf and d_crs == s_crs == g_crs):
        raise BadFile("the wind messages are not on the same grid")
    finite_d = d[np.isfinite(d)]
    if finite_d.size and (finite_d.min() < 0 or finite_d.max() > 360):
        raise BadFile(f"directions {finite_d.min():.0f}..{finite_d.max():.0f}° are out of range")

    rad = np.radians(d)
    # Components of the "from" direction, so atan2(east, north) is that direction.
    sources = {"east": s * np.sin(rad), "north": s * np.cos(rad), "speed": s, "gust": g}
    width, height, dst_transform = _out_grid(bounds)
    grid = {}
    for name, values in sources.items():
        out = np.full((height, width), np.nan)
        reproject(
            source=values, destination=out, src_transform=d_tf, src_crs=d_crs,
            src_nodata=np.nan, dst_transform=dst_transform, dst_crs="EPSG:4326",
            dst_nodata=np.nan, resampling=Resampling.bilinear,
        )
        grid[name] = out

    valid = np.isfinite(grid["speed"]) & np.isfinite(grid["east"]) & np.isfinite(grid["north"])
    share = float(valid.mean())
    if share < MIN_VALID_SHARE:
        raise BadFile(f"only {share:.0%} of the region has data")
    for name in ("speed", "gust"):
        v = grid[name][np.isfinite(grid[name])]
        if v.size and (v.min() < 0 or v.max() * MS_TO_MPH > MAX_MPH):
            raise BadFile(f"{name} {v.min():.1f}..{v.max():.1f} m/s is out of range")

    # Average each STEP_DEG block of the OUT_RES_DEG grid.
    n = round(wind.STEP_DEG / OUT_RES_DEG)
    ok = _blocks(np.where(valid, 1.0, np.nan), n)
    count = np.isfinite(ok).sum(-1)

    def mean(name: str) -> np.ndarray:
        b = _blocks(np.where(valid, grid[name], np.nan), n)
        return np.where(count > 0, np.nansum(b, -1) / np.maximum(count, 1), np.nan)

    east, north, mph = mean("east"), mean("north"), mean("speed") * MS_TO_MPH
    gusts = _blocks(grid["gust"], n)
    has_gust = np.isfinite(gusts).any(-1)
    gust_mph = np.where(has_gust, np.nanmax(np.where(np.isfinite(gusts), gusts, -1.0), -1), np.nan) * MS_TO_MPH

    west, _, _, north_edge = bounds
    features = []
    for row, col in zip(*np.nonzero(count > 0), strict=True):
        e, nn = float(east[row, col]), float(north[row, col])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [
                round(west + (col + 0.5) * wind.STEP_DEG, 3),
                round(north_edge - (row + 0.5) * wind.STEP_DEG, 3),
            ]},
            "properties": {
                # No direction for calm air (the components cancel out).
                "dir": round(math.degrees(math.atan2(e, nn))) % 360 if math.hypot(e, nn) >= 0.1 else None,
                "mph": round(float(mph[row, col])),
                "gust_mph": round(float(gust_mph[row, col])) if has_gust[row, col] else None,
                "lvl": _lvl(int(row), int(col)),
            },
        })
    body = json.dumps(
        {"type": "FeatureCollection", "valid": rasterlayer.iso(hour), "units": "mph",
         "features": features},
        separators=(",", ":"),
    ).encode()
    gusts_known = [f["properties"]["gust_mph"] for f in features if f["properties"]["gust_mph"] is not None]
    stats = {
        "max_mph": max(f["properties"]["mph"] for f in features),
        "max_gust_mph": max(gusts_known, default=None),
        "points": len(features),
    }
    return gzip.compress(body, 9, mtime=0), stats


# ── what the job publishes ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Product:
    name: str
    fields: tuple[Field, ...]
    prefix: Callable[[str], str]
    pointer_key: Callable[[str], str]
    object_key: Callable[[str, str], str]
    suffix: str
    content_type: str
    build: Callable[[dict[Field, bytes], datetime, tuple[float, float, float, float]], tuple[bytes, dict]]
    describe: Callable[[dict], str]


TEMPERATURE = Product(
    name="temperature",
    fields=(_FIELD,),
    prefix=temperature.prefix,
    pointer_key=temperature.pointer_key,
    object_key=temperature.cog_key,
    suffix=".tif",
    content_type="image/tiff",
    build=lambda m, hour, bounds: to_cog(m[_FIELD], hour, bounds),
    describe=lambda s: f"{s['min_f']}..{s['max_f']} °F",
)

WIND_ARROWS = Product(
    name="wind",
    fields=(WDIR, WIND, GUST),
    prefix=wind.prefix,
    pointer_key=wind.pointer_key,
    object_key=wind.arrows_key,
    suffix=".geojson.gz",
    content_type="application/gzip",
    build=lambda m, hour, bounds: to_arrows(m[WDIR], m[WIND], m[GUST], hour, bounds),
    describe=lambda s: f"{s['points']} arrows, up to {s['max_mph']} mph, gusts {s['max_gust_mph']}",
)

PRODUCTS = (TEMPERATURE, WIND_ARROWS)


# ── one hour from one source ──────────────────────────────────────────────────

async def fetch_messages(
    client: httpx.AsyncClient, upstream: str, base: str, hour: datetime,
    products: list[Product],
) -> tuple[dict[Field, bytes], dict[str, BadFile]] | None:
    """The messages `products` need for `hour`, plus the products whose
    fields the index lacks (each with why). None if `base` doesn't have
    that hour yet. Neighbouring messages are fetched in one request."""
    url = file_url(base, hour)
    status, idx, _ = await rasterlayer.download(
        client, f"{url}.idx", upstream=upstream, max_bytes=MAX_IDX_BYTES,
    )
    if status == 404:
        return None
    text = idx.decode("ascii", "replace")
    ranges: dict[Field, tuple[int, int]] = {}
    problems: dict[str, BadFile] = {}
    for product in products:
        try:
            wanted = {field: message_range(text, hour, field) for field in product.fields}
        except BadFile as exc:
            problems[product.name] = exc
            continue
        ranges.update(wanted)

    messages: dict[Field, bytes] = {}
    for lo, hi in merge_ranges(list(ranges.values())):
        status, body, _ = await rasterlayer.download(
            client, url, upstream=upstream, max_bytes=MAX_BYTES, byte_range=(lo, hi),
        )
        if status == 404:
            return None
        if len(body) != hi - lo + 1:
            raise BadFile(f"asked for {hi - lo + 1} bytes, got {len(body)}")
        for field, (flo, fhi) in ranges.items():
            if lo <= flo and fhi <= hi:
                messages[field] = body[flo - lo:fhi - lo + 1]
    return messages, problems


def _oldest_served(pointers: dict[str, dict]) -> datetime | None:
    served = [
        datetime.fromisoformat(p["current"]["valid"]) if "current" in p else None
        for p in pointers.values()
    ]
    return None if None in served else min(served, default=None)


def _publish(
    product: Product, messages: dict[Field, bytes], store: Store, regions: list[str],
    hour: datetime, upstream: str, now: datetime,
) -> dict[str, dict]:
    """Build and store `product` for every region. Raises BadFile."""
    digest = hashlib.sha256(b"".join(messages[f] for f in product.fields)).hexdigest()[:8]
    revision = f"{hour:%Y%m%d%H}-{digest}"
    new: dict[str, dict] = {}
    for region in regions:
        body, stats = product.build(messages, hour, REGIONS[region].max_bounds)
        key = product.object_key(region, revision)
        store.put(key, body, product.content_type)
        logger.info("%s %s: published %s (%s)", region, product.name, revision, product.describe(stats))
        new[region] = {
            "valid": rasterlayer.iso(hour),
            "revision": revision,
            "key": key,
            "source": upstream,
            "fetched": rasterlayer.iso(now),
            **stats,
        }
    return new


async def refresh(
    client: httpx.AsyncClient, store: Store, regions: list[str],
    pointers: dict[str, dict[str, dict]], now: datetime, sources=SOURCES,
    products: tuple[Product, ...] = PRODUCTS,
) -> tuple[dict[str, dict[str, dict]], bool]:
    """Publish, per product, the newest hour newer than what each region
    serves. `pointers` is {product: {region: latest.json}}. Returns
    ({product: {region: new entry}}, failed). One download serves every
    region, and every product that still needs that hour."""
    floors = {p.name: _oldest_served(pointers[p.name]) for p in products}
    pending = list(products)
    new: dict[str, dict[str, dict]] = {}
    # A rejected file always fails the run; an unreachable source only
    # if a product found nothing new (the other source may have it).
    failed = unreachable = False

    for hour in candidate_hours(now):
        for p in [p for p in pending if floors[p.name] and hour <= floors[p.name]]:
            logger.info("%s %s unchanged", p.name, f"{floors[p.name]:%Y%m%d%H}")
            pending.remove(p)
        for upstream, base in sources:
            if not pending:
                break
            try:
                got = await fetch_messages(client, upstream, base, hour, pending)
            except BadFile as exc:
                logger.error("%s %s: rejected: %s", upstream, f"{hour:%Y%m%d%H}", exc)
                failed = True
                continue
            except (httpx.HTTPError, CircuitOpenError) as exc:
                logger.warning("%s %s: %r", upstream, f"{hour:%Y%m%d%H}", exc)
                unreachable = True
                continue
            if got is None:
                continue
            messages, problems = got
            for p in list(pending):
                try:
                    if p.name in problems:
                        raise problems[p.name]
                    new[p.name] = _publish(p, messages, store, regions, hour, upstream, now)
                except BadFile as exc:
                    logger.error("%s %s %s: rejected: %s", upstream, f"{hour:%Y%m%d%H}", p.name, exc)
                    failed = True
                    continue
                pending.remove(p)
        if not pending:
            break

    for p in pending:
        if floors[p.name] is None:
            logger.error("no RTMA %s found in the last %s", p.name, LOOKBACK)
            failed = True
    unpublished = any(p.name not in new for p in products)
    return new, failed or (unreachable and unpublished)


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    now: datetime | None = None, sources=SOURCES, products: tuple[Product, ...] = PRODUCTS,
) -> bool:
    """Refresh every region. Returns True if all went well."""
    now = now or datetime.now(UTC)
    pointers = {
        p.name: {
            region: rasterlayer.load_pointer(store, p.pointer_key(region), {"region": region})
            for region in regions
        }
        for p in products
    }
    try:
        new, failed = await refresh(client, store, regions, pointers, now, sources, products)
    except Exception:
        logger.exception("refresh failed")
        new, failed = {}, True
    ok = not failed
    for p in products:
        for region, pointer in pointers[p.name].items():
            if region in new.get(p.name, {}):
                pointer["current"] = new[p.name][region]
                rasterlayer.save_pointer(store, p.pointer_key(region), pointer, now)
            try:
                keep = {pointer["current"]["key"]} if "current" in pointer else set()
                if n := rasterlayer.cleanup(store, f"{p.prefix(region)}/", keep, now - KEEP, p.suffix):
                    logger.info("%s %s: deleted %d old revisions", region, p.name, n)
            except Exception:
                logger.exception("%s %s: cleanup failed", region, p.name)
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
