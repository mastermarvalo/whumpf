"""Daily job: the "latest clear view" Sentinel-2 mosaic of each region.

    make sentinel-refresh                 (daily host cron, docs/runbook.md §19)
    make sentinel-refresh ARGS=--dry-run
    make sentinel-refresh ARGS="--trial=-105.9,39.7,-105.6,39.9"
    python -m app.jobs.sentinel_refresh [--region colorado] [--dry-run] [--rebuild]
                                        [--trial W,S,E,N] [--max-items N]

Keeps a working mosaic on local disk (WORK_DIR/<region>: true.tif,
false.tif, date.tif, state.json; uncompressed and sparse) and each run adds
the Earth Search scenes it hasn't seen from the last WINDOW_DAYS, newest
first. A pixel takes a scene's view only where the scene classification
calls it clear and the scene is newer than what the pixel has, so the
newest clear view always wins, scenes may arrive in any order, and a scene
whose clear pixels are all older is read no further than its SCL band. Then
it writes the three COGs (pixels older than WINDOW_DAYS left empty), uploads
them as a new revision and switches latest.json. A failure leaves the served
revision as it was; the next run picks up where this one stopped.

--dry-run does all of it in WORK_DIR/<region>-dry-run and uploads nothing.
--trial W,S,E,N does a dry run of just that box (WORK_DIR/trial), to look at
the output and time it. --rebuild starts the working mosaic from empty (also
automatic when the grid or the stretch changes). Exits 1 if anything failed.

Layout and pointer format: app/services/sentinel.py.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import math
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from app.http_retry import call_with_resilience
from app.regions import REGIONS
from app.services import rasterlayer, sentinel
from app.services.rasterlayer import DryRunStore, S3Store, Store
from app.services.sentinel import Grid

logger = logging.getLogger("whumpf.sentinel_refresh")

WORK_DIR = Path("/data/sentinel/work")
CHUNK = 2048            # pixels a side processed at once (~80 MB of arrays)
PAGE = 250
MAX_PAGES = 40
SKIP_CLOUD_COVER = 99.5  # scenes this cloudy aren't even opened
JPEG_QUALITY = 85
KEEP = timedelta(days=3)
MIN_COVERAGE = 0.2       # of the grid; less is not published (a broken run)

# Earth Search asset names -> what we read and how we resample to the grid.
# 10 m bands are averaged down to 12.5 m; 20 m bands are interpolated up.
BANDS = {
    "red": Resampling.average,
    "green": Resampling.average,
    "blue": Resampling.average,
    "nir08": Resampling.bilinear,
    "swir16": Resampling.bilinear,
}

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_USE_HEAD": "FALSE",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "AWS_NO_SIGN_REQUEST": "YES",
    "VSI_CACHE": "TRUE",
    "GDAL_NUM_THREADS": "2",
}


@dataclass(frozen=True)
class Scene:
    id: str
    when: datetime
    bbox: tuple[float, float, float, float]
    cloud_cover: float | None
    assets: dict[str, str]  # "scl" and BANDS keys -> href

    @property
    def day(self) -> date:
        return self.when.date()


def parse_scene(feature: dict) -> Scene | None:
    """A usable scene from an Earth Search item, or None (logged)."""
    assets = feature.get("assets", {})
    hrefs = {name: assets.get(name, {}).get("href", "") for name in ("scl", *BANDS)}
    if not all(h.startswith(sentinel.HREF_PREFIX) for h in hrefs.values()):
        logger.warning("skipping %s: an asset is missing or not on sentinel-cogs", feature.get("id"))
        return None
    props = feature.get("properties", {})
    try:
        when = datetime.fromisoformat(props["datetime"])
        bbox = tuple(float(v) for v in feature["bbox"][:4])
    except (KeyError, ValueError, TypeError):
        logger.warning("skipping %s: no usable datetime or bbox", feature.get("id"))
        return None
    return Scene(feature["id"], when, bbox, props.get("eo:cloud_cover"), hrefs)  # type: ignore[arg-type]


async def search(client: httpx.AsyncClient, bbox: tuple[float, float, float, float],
                 start: datetime, end: datetime, url: str = sentinel.EARTH_SEARCH_URL) -> list[Scene]:
    """Every scene touching bbox between start and end, newest first."""
    body: dict | None = {"collections": [sentinel.COLLECTION], "bbox": list(bbox), "limit": PAGE,
                         "datetime": f"{rasterlayer.iso(start)}/{rasterlayer.iso(end)}"}
    scenes: dict[str, Scene] = {}
    for _ in range(MAX_PAGES):
        async def _do(body=body, url=url) -> httpx.Response:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return r
        page = (await call_with_resilience("earth_search", _do, attempts=4, retry_429=True)).json()
        for feature in page.get("features", []):
            if (scene := parse_scene(feature)) is not None:
                scenes[scene.id] = scene
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if nxt is None:
            return sorted(scenes.values(), key=lambda s: (s.when, s.id), reverse=True)
        url, body = nxt["href"], nxt.get("body", body)
    raise RuntimeError(f"more than {MAX_PAGES} pages of scenes")


# ── the working mosaic ────────────────────────────────────────────────────────

class Mosaic:
    """WORK_DIR/<name>: true.tif and false.tif (3 x uint8), date.tif (uint16
    day numbers, 0 = none) and state.json (grid, stretch, scenes done)."""

    def __init__(self, path: Path, grid: Grid, rebuild: bool = False) -> None:
        self.path, self.grid = path, grid
        path.mkdir(parents=True, exist_ok=True)
        self.state_file = path / "state.json"
        state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        fresh = rebuild or state.get("grid") != grid.key() or state.get("render") != sentinel.RENDER
        if fresh:
            if state:
                logger.info("%s: starting the working mosaic from empty", path.name)
            for name in ("true.tif", "false.tif", "date.tif", "state.json"):
                (path / name).unlink(missing_ok=True)
            state = {}
        self.done: dict[str, str] = state.get("done", {})
        profile = {"driver": "GTiff", "width": grid.width, "height": grid.height, "crs": grid.crs,
                   "transform": grid.transform, "tiled": True, "blockxsize": 512, "blockysize": 512,
                   "BIGTIFF": "YES", "SPARSE_OK": "TRUE"}
        for name, count, dtype in (("true", 3, "uint8"), ("false", 3, "uint8"), ("date", 1, "uint16")):
            file = path / f"{name}.tif"
            if not file.exists():
                with rasterio.open(file, "w", count=count, dtype=dtype, **profile):
                    pass
        self.ds = {name: rasterio.open(path / f"{name}.tif", "r+") for name in ("true", "false", "date")}

    def save(self, keep_after: date) -> None:
        self.done = {k: v for k, v in self.done.items() if v >= keep_after.isoformat()}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"grid": self.grid.key(), "render": sentinel.RENDER, "done": self.done}))
        tmp.replace(self.state_file)

    def close(self) -> None:
        for ds in self.ds.values():
            ds.close()


def chunks(col0: int, row0: int, col1: int, row1: int):
    for r in range(row0, row1, CHUNK):
        for c in range(col0, col1, CHUNK):
            yield Window(c, r, min(CHUNK, col1 - c), min(CHUNK, row1 - r))


def _url(href: str) -> str:
    return f"/vsicurl/{href}" if href.startswith("https://") else href  # a local file in tests


def _vrt(src, grid: Grid, resampling: Resampling) -> WarpedVRT:
    return WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height,
                     resampling=resampling, src_nodata=0, nodata=0)


def apply_scene(mosaic: Mosaic, scene: Scene) -> int:
    """Blocking: take the scene's clear pixels that are newer than the
    mosaic's. Returns how many pixels changed. Raises if a read fails."""
    grid = mosaic.grid
    span = grid.window_of(scene.bbox)
    if span is None:
        return 0
    day = sentinel.day_number(scene.day)
    changed = 0
    # Env is per thread: this runs in a worker thread.
    with rasterio.Env(**GDAL_ENV), rasterio.open(_url(scene.assets["scl"])) as scl_src, \
            _vrt(scl_src, grid, Resampling.nearest) as scl_vrt:
        bands: dict[str, tuple] = {}
        try:
            for win in chunks(*span):
                scl = scl_vrt.read(1, window=win)
                clear = np.isin(scl, sentinel.CLEAR_SCL)
                if not clear.any():
                    continue
                current = mosaic.ds["date"].read(1, window=win)
                take = clear & (current < day)
                if not take.any():
                    continue
                if not bands:
                    for name, resampling in BANDS.items():
                        src = rasterio.open(_url(scene.assets[name]))
                        bands[name] = (src, _vrt(src, grid, resampling))
                values = {name: vrt.read(1, window=win) for name, (_, vrt) in bands.items()}
                # A clear SCL pixel with no reflectance (an edge) is not a view.
                take &= values["red"] > 0
                if not take.any():
                    continue
                for product, rgb in sentinel.render(values).items():
                    old = mosaic.ds[product].read(window=win)
                    old[:, take] = rgb[:, take]
                    mosaic.ds[product].write(old, window=win)
                current[take] = day
                mosaic.ds["date"].write(current, 1, window=win)
                changed += int(take.sum())
        finally:
            for src, vrt in bands.values():
                vrt.close()
                src.close()
    return changed


# ── the COGs ─────────────────────────────────────────────────────────────────

@dataclass
class Exported:
    paths: dict[str, Path]  # true, false, date (COGs) and dates (.json.gz)
    coverage: float
    dates: list[str] | None


def dates_grid(date_cog: Path, bbox: tuple[float, float, float, float]) -> dict:
    """Blocking: the dates grid over bbox (lon/lat), the most common capture
    day in each cell."""
    cell = sentinel.DATES_CELL_DEG
    west, north = math.floor(bbox[0] / cell) * cell, math.ceil(bbox[3] / cell) * cell
    width = math.ceil((bbox[2] - west) / cell)
    height = math.ceil((north - bbox[1]) / cell)
    with rasterio.open(date_cog) as src, WarpedVRT(
            src, crs="EPSG:4326", transform=from_origin(west, north, cell, cell), width=width, height=height,
            resampling=Resampling.mode, src_nodata=0, nodata=0) as vrt:
        days = vrt.read(1)
    return sentinel.dates_doc(days, west, north, cell)


def export(mosaic: Mosaic, out: Path, cutoff: date, bbox: tuple[float, float, float, float]) -> Exported:
    """Blocking: the three COGs from the mosaic, pixels older than cutoff empty."""
    out.mkdir(parents=True, exist_ok=True)
    grid = mosaic.grid
    first = sentinel.day_number(cutoff)
    base = {"driver": "GTiff", "width": grid.width, "height": grid.height, "crs": grid.crs,
            "transform": grid.transform, "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "YES"}
    staged = {name: out / f"{name}-src.tif" for name in ("true", "false", "date")}
    shown, low, high = 0, None, None
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK="YES"), \
            rasterio.open(staged["true"], "w", count=3, dtype="uint8", **base) as t, \
            rasterio.open(staged["false"], "w", count=3, dtype="uint8", **base) as f, \
            rasterio.open(staged["date"], "w", count=1, dtype="uint16", nodata=0, **base) as d:
        for win in chunks(0, 0, grid.width, grid.height):
            days = mosaic.ds["date"].read(1, window=win)
            valid = days >= first
            if valid.any():
                shown += int(valid.sum())
                low = min(low or 1 << 30, int(days[valid].min()))
                high = max(high or 0, int(days[valid].max()))
            mask = np.where(valid, 255, 0).astype(np.uint8)
            for product, dst in (("true", t), ("false", f)):
                dst.write(mosaic.ds[product].read(window=win), window=win)
                dst.write_mask(mask, window=win)
            d.write(np.where(valid, days, 0).astype(np.uint16), 1, window=win)
    paths = {name: out / f"{name}.tif" for name in ("true", "false", "date")}
    for product in sentinel.PRODUCTS:
        rasterlayer.to_cog(str(staged[product]), str(paths[product]), compress="JPEG", quality=JPEG_QUALITY)
    rasterlayer.to_cog(str(staged["date"]), str(paths["date"]), compress="DEFLATE", predictor=2,
                       resampling="nearest")
    for path in staged.values():
        path.unlink()
    paths["dates"] = out / "dates.json.gz"
    doc = dates_grid(paths["date"], bbox)
    paths["dates"].write_bytes(gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0))
    dates = ([sentinel.from_day_number(low).isoformat(), sentinel.from_day_number(high).isoformat()]
             if shown else None)
    return Exported(paths, shown / (grid.width * grid.height), dates)


# ── a run ────────────────────────────────────────────────────────────────────

def _work_name(region: str, dry_run: bool) -> str:
    return f"{region}-dry-run" if dry_run else region


async def refresh_region(client: httpx.AsyncClient, store: Store, region: str, now: datetime, *,
                         bbox: tuple[float, float, float, float] | None = None, work_name: str | None = None,
                         rebuild: bool = False, max_items: int | None = None, keep_output: bool = False,
                         work_dir: Path = WORK_DIR, search_url: str = sentinel.EARTH_SEARCH_URL) -> dict:
    """Updates the working mosaic, uploads the COGs through `store` and
    returns the new pointer entry. Raises if it can't be published."""
    grid = Grid(bbox or REGIONS[region].bbox)
    today = now.date()
    cutoff = today - timedelta(days=sentinel.WINDOW_DAYS)
    started = time.monotonic()
    scenes = await search(client, bbox or REGIONS[region].bbox,
                          datetime.combine(cutoff, datetime.min.time(), UTC), now, search_url)
    mosaic = await asyncio.to_thread(Mosaic, work_dir / (work_name or region), grid, rebuild)
    try:
        todo = [s for s in scenes if s.id not in mosaic.done]
        if max_items is not None:
            todo = todo[:max_items]
        logger.info("%s: %d scenes in the last %d days, %d new; grid %d x %d", region, len(scenes),
                    sentinel.WINDOW_DAYS, len(todo), grid.width, grid.height)
        for i, scene in enumerate(todo, 1):
            if scene.cloud_cover is not None and scene.cloud_cover >= SKIP_CLOUD_COVER:
                mosaic.done[scene.id] = scene.day.isoformat()
                continue
            t0 = time.monotonic()
            changed = await asyncio.to_thread(apply_scene, mosaic, scene)
            mosaic.done[scene.id] = scene.day.isoformat()
            logger.info("%s: [%d/%d] %s (%.0f%% cloud): %d pixels in %.1f s", region, i, len(todo),
                        scene.id, scene.cloud_cover or 0, changed, time.monotonic() - t0)
            if i % 10 == 0:
                mosaic.save(cutoff)
        mosaic.save(cutoff)

        t0 = time.monotonic()
        out = work_dir / f"{work_name or region}-out"
        exported = await asyncio.to_thread(export, mosaic, out, cutoff, bbox or REGIONS[region].bbox)
    finally:
        mosaic.close()
    sizes = {name: path.stat().st_size for name, path in exported.paths.items()}
    logger.info("%s: COGs in %.0f s: %s bytes, %.1f%% of the grid shown, dates %s", region,
                time.monotonic() - t0, sizes, 100 * exported.coverage, exported.dates)
    if exported.coverage < MIN_COVERAGE:
        raise RuntimeError(f"{region}: only {100 * exported.coverage:.1f}% of the grid has a view; not publishing")

    revision = f"{now:%Y%m%d-%H%M}"
    keys = {name: sentinel.cog_key(region, name, revision) for name in ("true", "false", "date")}
    keys["dates"] = sentinel.dates_key(region, revision)
    for name, path in exported.paths.items():
        await asyncio.to_thread(store.put_file, keys[name], str(path),
                                "application/json" if name == "dates" else "image/tiff")
    if not keep_output:
        shutil.rmtree(out, ignore_errors=True)
    newest = max((s.when for s in scenes), default=None)
    logger.info("%s: revision %s in %.0f s", region, revision, time.monotonic() - started)
    return {"revision": revision, "keys": keys, "key": keys["true"], "valid": rasterlayer.iso(now),
            "window_days": sentinel.WINDOW_DAYS, "dates": exported.dates,
            "newest_scene": rasterlayer.iso(newest) if newest else None,
            "coverage": round(exported.coverage, 4), "render": sentinel.RENDER,
            "bytes": sum(sizes.values())}


async def run(store: Store, client: httpx.AsyncClient, regions: list[str], now: datetime | None = None,
              *, dry_run: bool = False, rebuild: bool = False, max_items: int | None = None,
              work_dir: Path = WORK_DIR, search_url: str = sentinel.EARTH_SEARCH_URL) -> bool:
    """Refresh every region. Returns True if all went well."""
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        pointer = rasterlayer.load_pointer(store, sentinel.pointer_key(region), {"region": region})
        try:
            new = await refresh_region(client, store, region, now, work_name=_work_name(region, dry_run),
                                       rebuild=rebuild, max_items=max_items, work_dir=work_dir,
                                       search_url=search_url)
        except Exception:
            logger.exception("%s: refresh failed", region)
            new, ok = None, False
        if new is not None:
            pointer["current"] = new
            rasterlayer.save_pointer(store, sentinel.pointer_key(region), pointer, now)
        try:
            current = pointer.get("current", {})
            keep = set(current.get("keys", {}).values())
            if n := rasterlayer.cleanup(store, f"{sentinel.prefix(region)}/", keep, now - KEEP):
                logger.info("%s: deleted %d old files", region, n)
        except Exception:
            logger.exception("%s: cleanup failed", region)
            ok = False
    return ok


async def trial(client: httpx.AsyncClient, bbox: tuple[float, float, float, float], now: datetime,
                rebuild: bool, max_items: int | None) -> bool:
    """A dry run of one box; the COGs stay in WORK_DIR/trial-out to look at."""
    try:
        entry = await refresh_region(client, DryRunStore(S3Store()), "colorado", now, bbox=bbox,
                                     work_name="trial", rebuild=rebuild, max_items=max_items, keep_output=True)
    except Exception:
        logger.exception("trial failed")
        return False
    out = WORK_DIR / "trial-out"
    for product in sentinel.PRODUCTS:
        await asyncio.to_thread(preview, out / f"{product}.tif", out / f"{product}-preview.jpg")
    logger.info("trial: %s; files (and -preview.jpg) in %s", json.dumps(entry), out)
    return True


def preview(cog: Path, jpg: Path, px: int = 1024) -> None:
    """Blocking: a JPEG at most px wide of an RGB COG, masked areas black."""
    from PIL import Image
    with rasterio.open(cog) as src:
        scale = min(1.0, px / max(src.width, src.height))
        shape = (max(1, round(src.height * scale)), max(1, round(src.width * scale)))
        rgb = src.read(out_shape=(3, *shape), resampling=Resampling.average)
    Image.fromarray(np.moveaxis(rgb, 0, -1)).save(jpg, quality=85)


async def _main(args: argparse.Namespace) -> bool:
    headers = {"User-Agent": "whumpf/0.1 (+https://whumpf.co)"}
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        if args.trial:
            bbox = tuple(float(v) for v in args.trial.split(","))
            return await trial(client, bbox, datetime.now(UTC), args.rebuild, args.max_items)  # type: ignore[arg-type]
        store: Store = DryRunStore(S3Store()) if args.dry_run else S3Store()
        return await run(store, client, args.region or sorted(REGIONS), dry_run=args.dry_run,
                         rebuild=args.rebuild, max_items=args.max_items)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", choices=sorted(REGIONS), action="append", help="default: every region")
    parser.add_argument("--dry-run", action="store_true",
                        help="build in a separate working mosaic and upload nothing")
    parser.add_argument("--rebuild", action="store_true", help="start the working mosaic from empty")
    parser.add_argument("--trial", metavar="W,S,E,N", help="dry run of just this box, output kept")
    parser.add_argument("--max-items", type=int, help="process at most this many new scenes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(0 if asyncio.run(_main(args)) else 1)


if __name__ == "__main__":
    main()
