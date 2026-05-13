#!/usr/bin/env python3
"""
DEM pipeline for Whumpf.

Downloads USGS 3DEP DEMs (1/3 arc-second ~10m or 1-meter lidar), mosaics and
reprojects to EPSG:3857, computes hillshade / slope / aspect, converts all
outputs to Cloud Optimized GeoTIFF, and uploads to MinIO dem-cogs.

Usage:
    python dem_pipeline.py [--bbox W,S,E,N] [--region PREFIX] [--workdir DIR]
                           [--resolution 1/3|1m] [--strip-height N]

For 1m resolution: gdalwarp and gdalbuildvrt must be on PATH.
    sudo apt-get install -y gdal-bin

Deps (install into a local venv first):
    pip install -r data/pipelines/requirements.txt
"""

import argparse
import gc
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import boto3
    import httpx
    import numpy as np
    import rasterio
    from botocore.client import Config
    from rasterio import crs as rcrs
    from rasterio import warp as rwarp
    from rasterio.enums import Resampling
    from rasterio.merge import merge as rio_merge
    from rasterio.shutil import copy as rio_copy
    from rasterio.windows import Window
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    print("Run:  pip install rasterio numpy boto3 httpx")
    sys.exit(1)

# ── constants ──────────────────────────────────────────────────────────────────

DEFAULT_BBOX = (-108.5, 37.0, -106.5, 38.5)   # San Juan Mountains, CO
TNM_API      = "https://tnmaccess.nationalmap.gov/api/v1/products"
OUTPUT_CRS   = "EPSG:3857"
MINIO_BUCKET = "dem-cogs"

PRIORITY_STACK_RES_M      = 10.0   # baseline output resolution for priority-blend DEM
HIRES_MAX_UNCOMPRESSED_GB = 500.0  # skip hires COG only if estimated raw size exceeds this

# Per-resolution TNM dataset names and deduplication strategy.
RESOLUTION_CONFIG = {
    "1/3": {
        "dataset":      "National Elevation Dataset (NED)",
        "title_filter": "1/3 Arc Second",   # substring match in tile title
        "dedup":        "degree_cell",       # extract n37w107 key
    },
    "1m": {
        "dataset":      "Digital Elevation Model (DEM) 1 meter",
        "title_filter": None,                # all items in this dataset are 1m
        "dedup":        "filename",          # deduplicate by download URL basename
    },
}

MAX_RETRIES        = 6      # download attempts per file
RETRY_BASE_SLEEP   = 4      # seconds (doubles each retry)
DOWNLOAD_CHUNK     = 256 * 1024   # 256 KB stream chunks

# ── logging ────────────────────────────────────────────────────────────────────

logger = logging.getLogger("dem_pipeline")


def setup_logging(log_path: Path) -> None:
    """Log to stdout and to log_path simultaneously, with timestamps."""
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.DEBUG)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info("Log file: %s", log_path)


def step(msg: str) -> None:
    logger.info("── %s", msg)


# ── env helpers ────────────────────────────────────────────────────────────────

def load_env(path: str) -> dict:
    """Parse a .env file; environment variables take precedence."""
    env: dict = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith("MINIO_")})
    return env


# ── pre-flight checks ──────────────────────────────────────────────────────────

def check_disk_space(path: Path, need_gb: float) -> None:
    """Exit early if available disk space is below need_gb."""
    stat = shutil.disk_usage(path)
    free_gb = stat.free / 1e9
    logger.info("Disk space at %s: %.1f GB free", path, free_gb)
    if free_gb < need_gb:
        sys.exit(
            f"ERROR: only {free_gb:.1f} GB free at {path}; "
            f"need at least {need_gb:.0f} GB. "
            "Expand the disk or point --workdir to a larger volume."
        )


def check_gdalwarp() -> bool:
    """Return True if gdalwarp and gdalbuildvrt are on PATH."""
    ok = shutil.which("gdalwarp") and shutil.which("gdalbuildvrt")
    if not ok:
        logger.warning(
            "gdalwarp/gdalbuildvrt not found. "
            "Required for 1m data: sudo apt-get install -y gdal-bin"
        )
    return bool(ok)


# ── TNM query ──────────────────────────────────────────────────────────────────

def query_tnm(bbox: tuple, resolution: str = "1/3") -> list[dict]:
    """
    Return deduplicated [{title, url}] for the given resolution covering bbox.

    1/3 arc-second: one tile per degree cell, newest version wins.
    1m: one entry per unique download URL (USGS organises these by project quad).
    """
    import re
    cfg = RESOLUTION_CONFIG[resolution]
    dataset = cfg["dataset"]
    step(f"Querying TNM  bbox={bbox}  resolution={resolution!r}  dataset={dataset!r}")

    w, s, e, n = bbox
    params = {
        "datasets":     dataset,
        "bbox":         f"{w},{s},{e},{n}",
        "prodFormats":  "GeoTIFF",
        "outputFormat": "JSON",
        "max":          200,
        "offset":       0,
    }
    raw: list[dict] = []
    while True:
        for attempt in range(MAX_RETRIES):
            try:
                resp = httpx.get(TNM_API, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                wait = RETRY_BASE_SLEEP * (2 ** attempt)
                logger.warning("TNM API attempt %d/%d failed: %r — retry in %ds",
                               attempt + 1, MAX_RETRIES, exc, wait)
                if attempt == MAX_RETRIES - 1:
                    raise RuntimeError(f"TNM API unreachable after {MAX_RETRIES} attempts") from exc
                time.sleep(wait)

        items = data.get("items", [])
        total = data.get("total", 0)
        raw.extend(items)
        logger.info("  fetched %d / %d", len(raw), total)
        if len(raw) >= total or not items:
            break
        params["offset"] = len(raw)

    # Filter to GeoTIFF URLs and optional title substring
    title_filter = cfg.get("title_filter")
    candidates = [
        it for it in raw
        if it.get("downloadURL", "").lower().endswith((".tif", ".tiff"))
        and (title_filter is None or title_filter in it.get("title", ""))
    ]

    # Deduplicate
    if cfg["dedup"] == "degree_cell":
        best: dict[str, tuple[str, dict]] = {}
        for it in candidates:
            m = re.search(r"(n\d+w\d+)", it["title"])
            cell = m.group(1) if m else it["title"]
            m2 = re.search(r"(\d{8})\s*$", it["title"].strip())
            date = m2.group(1) if m2 else "00000000"
            if cell not in best or date > best[cell][0]:
                best[cell] = (date, it)
        candidates = [v for _, v in best.values()]
    else:
        # 1m: deduplicate by URL basename
        seen: dict[str, dict] = {}
        for it in candidates:
            key = it["downloadURL"].split("/")[-1].split("?")[0]
            seen[key] = it   # last one wins (fine; no version ambiguity here)
        candidates = list(seen.values())

    result = sorted(
        [{"title": it["title"], "url": it["downloadURL"]} for it in candidates],
        key=lambda x: x["title"],
    )

    if resolution == "1/3":
        result = _supplement_missing_13_tiles(bbox, result)

    logger.info("%d tiles to download", len(result))
    return result


def _supplement_missing_13_tiles(bbox: tuple, tiles: list[dict]) -> list[dict]:
    """Fill degree-cell gaps in TNM results by checking the USGS S3 current/ prefix.

    TNM API pagination can silently drop tiles when pages time out.  We compute
    every expected nNNwNNN cell for the bbox and verify each one is covered.
    Missing cells are fetched directly from the S3 staging bucket.
    """
    import math, re

    USGS_S3 = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF"

    w, s, e, n = bbox
    # Cells needed: north edge ∈ [ceil(s)+0 … ceil(n)], west edge ∈ [ceil(|w|) … ceil(|e|)+1]
    north_edges = range(math.floor(s) + 1, math.ceil(n) + 1)
    west_edges  = range(math.floor(abs(e)), math.ceil(abs(w)) + 1)
    expected_cells = {f"n{lat:02d}w{lon:03d}" for lat in north_edges for lon in west_edges}

    found_cells: set[str] = set()
    for t in tiles:
        m = re.search(r"(n\d+w\d+)", t["title"])
        if m:
            found_cells.add(m.group(1))

    missing = expected_cells - found_cells
    if not missing:
        return tiles

    logger.warning("TNM result missing %d degree cell(s): %s — checking S3 current/ directly",
                   len(missing), ", ".join(sorted(missing)))

    extra: list[dict] = []
    for cell in sorted(missing):
        # Try current/ first (single canonical file, no date suffix)
        for path_prefix in (f"{USGS_S3}/current/{cell}", f"{USGS_S3}/historical/{cell}"):
            listing_url = (
                f"https://prd-tnm.s3.amazonaws.com/"
                f"?list-type=2&prefix=StagedProducts%2FElevation%2F13%2FTIFF%2F"
                f"{path_prefix.split('/TIFF/')[-1].replace('/', '%2F')}%2F&max-keys=20"
            )
            try:
                r = httpx.get(listing_url, timeout=20)
                # [^<>]* prevents the regex from spanning across XML tags
                tif_keys = re.findall(r"<Key>([^<>]*?\.tif)</Key>", r.text, re.IGNORECASE)
                if tif_keys:
                    # Pick the most recent (last alphabetically)
                    key = sorted(tif_keys)[-1]
                    url = f"https://prd-tnm.s3.amazonaws.com/{key}"
                    fname = key.split("/")[-1]
                    extra.append({"title": f"USGS 13 {cell} [S3-supplement]", "url": url,
                                  "optional": True})
                    logger.info("  supplemented %s → %s", cell, fname)
                    break
            except Exception as exc:
                logger.debug("S3 supplement lookup failed for %s: %s", cell, exc)

    return tiles + extra


# ── download ───────────────────────────────────────────────────────────────────

def _remote_size(url: str) -> int | None:
    """Return Content-Length of url via HEAD, or None if unavailable."""
    try:
        r = httpx.head(url, timeout=15, follow_redirects=True)
        cl = r.headers.get("content-length")
        return int(cl) if cl else None
    except Exception:
        return None


def download_tiles(tiles: list[dict], dest: Path) -> list[Path]:
    """
    Download each tile with resume and per-file retry.

    Uses a .part file while downloading; renames to final path on success.
    If the .part file already exists, resumes from its current size using
    an HTTP Range request (server must support 206 Partial Content).
    """
    step(f"Downloading {len(tiles)} tile(s) → {dest}")
    paths: list[Path] = []

    for i, tile in enumerate(tiles, 1):
        url = tile["url"]
        fname = url.split("/")[-1].split("?")[0] or f"tile_{i}.tif"
        out  = dest / fname
        part = dest / (fname + ".part")

        if out.exists():
            logger.info("  [%d/%d] cached   %s", i, len(tiles), fname)
            paths.append(out)
            continue

        # How large should the final file be?
        remote_bytes = _remote_size(url)

        for attempt in range(MAX_RETRIES):
            try:
                existing = part.stat().st_size if part.exists() else 0

                # Only attempt resume if server reported a size to validate against
                can_resume = existing > 0 and remote_bytes is not None and existing < remote_bytes
                headers = {"Range": f"bytes={existing}-"} if can_resume else {}
                if can_resume:
                    logger.info("  [%d/%d] resuming %s from %.1f MB",
                                i, len(tiles), fname, existing / 1e6)
                else:
                    if attempt == 0:
                        logger.info("  [%d/%d] %s  (%.0f MB)",
                                    i, len(tiles), fname,
                                    (remote_bytes or 0) / 1e6)
                    existing = 0  # restart

                t0 = time.time()
                with httpx.stream("GET", url, timeout=600, follow_redirects=True,
                                  headers=headers) as r:
                    r.raise_for_status()
                    mode = "ab" if (can_resume and r.status_code == 206) else "wb"
                    if mode == "wb":
                        existing = 0
                    with part.open(mode) as f:
                        written = 0
                        for chunk in r.iter_bytes(chunk_size=DOWNLOAD_CHUNK):
                            f.write(chunk)
                            written += len(chunk)
                        total_written = existing + written

                elapsed = time.time() - t0
                mbps = written / 1e6 / max(elapsed, 0.1)
                logger.info("  [%d/%d] done  %.1f MB  (%.1f MB/s)",
                            i, len(tiles), total_written / 1e6, mbps)
                part.rename(out)
                break

            except Exception as exc:
                wait = RETRY_BASE_SLEEP * (2 ** attempt)
                logger.warning(
                    "  [%d/%d] attempt %d/%d failed: %r — retry in %ds",
                    i, len(tiles), attempt + 1, MAX_RETRIES, exc, wait,
                )
                if attempt == MAX_RETRIES - 1:
                    if tile.get("optional"):
                        logger.warning("  [%d/%d] skipping optional tile %s (not available)",
                                       i, len(tiles), fname)
                        part.unlink(missing_ok=True)
                        out = None  # sentinel: tile was skipped
                        break
                    # Leave .part on disk for next run to resume
                    raise RuntimeError(f"Download failed after {MAX_RETRIES} attempts: {url}") from exc
                time.sleep(wait)

        if out is not None:
            paths.append(out)

    return paths


# ── mosaic + reproject ─────────────────────────────────────────────────────────

def mosaic_and_reproject(
    paths: list[Path],
    out_path: Path,
    clip_bounds: tuple | None = None,
    use_gdalwarp: bool = False,
) -> tuple:
    """
    Merge source tiles and reproject to EPSG:3857, write as COG.

    Returns (None, profile, transform).  The data array is NOT kept in memory;
    use compute_derivatives_windowed to read out_path in strips.

    use_gdalwarp=True uses subprocess gdalbuildvrt + gdalwarp, which streams
    data from disk and is the only viable option for 1m-resolution inputs.
    use_gdalwarp=False uses rasterio.merge (loads full mosaic into RAM —
    fine for the ~200 MB 10m case, fatal for 1m).
    """
    if out_path.exists():
        step(f"dem.tif exists — reading metadata from {out_path.name}")
        with rasterio.open(out_path) as ds:
            tf = ds.transform
            profile = {
                "driver": "GTiff", "dtype": ds.dtypes[0],
                "width": ds.width, "height": ds.height,
                "count": 1, "crs": ds.crs, "transform": tf,
                "nodata": ds.nodata,
            }
        logger.info("  %d × %d  nodata=%s", profile["width"], profile["height"], profile["nodata"])
        return None, profile, tf

    if use_gdalwarp:
        _mosaic_gdalwarp(paths, out_path, clip_bounds)
    else:
        _mosaic_rasterio(paths, out_path, clip_bounds)

    with rasterio.open(out_path) as ds:
        tf = ds.transform
        profile = {
            "driver": "GTiff", "dtype": ds.dtypes[0],
            "width": ds.width, "height": ds.height,
            "count": 1, "crs": ds.crs, "transform": tf,
            "nodata": ds.nodata,
        }
    logger.info("  DEM: %d × %d px  res=%.2f m  nodata=%s",
                profile["width"], profile["height"],
                abs(tf.a), profile["nodata"])
    return None, profile, tf


def _mosaic_gdalwarp(paths: list[Path], out_path: Path, clip_bounds: tuple | None) -> None:
    """gdalbuildvrt + gdalwarp: streams data, minimal RAM, required for 1m."""
    step(f"Building VRT from {len(paths)} tiles ...")
    vrt = out_path.with_suffix(".vrt")
    cmd_vrt = ["gdalbuildvrt", "-r", "bilinear", str(vrt)] + [str(p) for p in paths]
    logger.info("  %s", " ".join(cmd_vrt))
    subprocess.run(cmd_vrt, check=True)

    step("Reprojecting → EPSG:3857 (gdalwarp, streaming) ...")
    tmp = out_path.with_suffix(".tmp.tif")
    cmd_warp = [
        "gdalwarp",
        "-t_srs", OUTPUT_CRS,
        "-r", "bilinear",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=YES",
        "-co", "BLOCKXSIZE=512",
        "-co", "BLOCKYSIZE=512",
        "-wm", "2048",       # warp memory (MB)
        "-multi",            # multi-threaded I/O
        "-overwrite",
    ]
    if clip_bounds:
        w, s, e, n = clip_bounds
        cmd_warp += ["-te", str(w), str(s), str(e), str(n), "-te_srs", "EPSG:4326"]
    cmd_warp += [str(vrt), str(tmp)]
    logger.info("  %s", " ".join(cmd_warp))
    subprocess.run(cmd_warp, check=True)

    # Build overviews in-place, then copy to proper COG layout.
    step("Building overviews ...")
    cmd_addo = ["gdaladdo", "-r", "average", str(tmp), "2", "4", "8", "16", "32", "64"]
    subprocess.run(cmd_addo, check=True)

    step(f"Finalizing DEM COG → {out_path.name} ...")
    rio_copy(str(tmp), str(out_path),
             copy_src_overviews=True,
             driver="GTiff", tiled=True,
             blockxsize=512, blockysize=512,
             compress="deflate", BIGTIFF="YES")
    tmp.unlink(missing_ok=True)
    vrt.unlink(missing_ok=True)


def _mosaic_rasterio(paths: list[Path], out_path: Path, clip_bounds: tuple | None) -> None:
    """In-memory rasterio merge — fine for 10m (~200 MB result), not for 1m."""
    import contextlib
    step(f"Mosaicking {len(paths)} tiles (in-memory) ...")
    merge_kwargs: dict = {}
    if clip_bounds is not None:
        merge_kwargs["bounds"] = clip_bounds
    with contextlib.ExitStack() as stack:
        srcs = [stack.enter_context(rasterio.open(p)) for p in paths]
        src_crs   = srcs[0].crs
        src_nodata = srcs[0].nodata
        mosaic, out_tf = rio_merge(srcs, **merge_kwargs)
    logger.info("  mosaic %s  crs=%s  nodata=%s", mosaic.shape, src_crs, src_nodata)

    step(f"Reprojecting to {OUTPUT_CRS} ...")
    dst_crs = rcrs.CRS.from_string(OUTPUT_CRS)
    _, nrows, ncols = mosaic.shape
    left, bottom, right, top = rasterio.transform.array_bounds(nrows, ncols, out_tf)
    dst_tf, dst_w, dst_h = rwarp.calculate_default_transform(
        src_crs, dst_crs, ncols, nrows,
        left=left, bottom=bottom, right=right, top=top,
    )
    dst = np.empty((1, dst_h, dst_w), dtype=mosaic.dtype)
    rwarp.reproject(
        source=mosaic, destination=dst,
        src_transform=out_tf, src_crs=src_crs,
        dst_transform=dst_tf, dst_crs=dst_crs,
        resampling=Resampling.bilinear,
        src_nodata=src_nodata, dst_nodata=src_nodata,
    )
    del mosaic
    gc.collect()

    profile = {
        "driver": "GTiff", "dtype": dst.dtype,
        "width": dst_w, "height": dst_h,
        "count": 1, "crs": dst_crs,
        "transform": dst_tf, "nodata": src_nodata,
    }
    write_cog(dst, profile, out_path)
    del dst
    gc.collect()


# ── priority-stack helpers ─────────────────────────────────────────────────────

def _build_vrt(paths: list[Path], vrt_path: Path,
               clip_bounds: tuple | None = None) -> None:
    """Build a GDAL VRT. Last-listed source wins on pixel overlap.

    clip_bounds (W, S, E, N) in WGS84: passed as -te to gdalbuildvrt so only
    tiles intersecting that extent are included.  Useful for hires sub-regions.

    -allow_projection_difference is required for 1m lidar tiles which span the
    UTM Zone 12N/13N boundary (at 108°W across Colorado). gdalwarp handles the
    reprojection to EPSG:3857, so mixed-CRS input is fine here.
    """
    step(f"Building VRT from {len(paths)} file(s) → {vrt_path.name} ...")
    cmd = ["gdalbuildvrt", "-r", "bilinear", "-allow_projection_difference"]
    # Do NOT pass -te here: gdalbuildvrt 3.4 has no -te_srs, so WGS84 bounds
    # are misinterpreted as UTM coordinates when tiles are in a projected CRS,
    # producing an empty VRT and all-nodata output.  gdalwarp clips correctly
    # via its own -te/-te_srs EPSG:4326 flags.
    cmd += [str(vrt_path)] + [str(p) for p in paths]
    logger.info("  gdalbuildvrt ... [%d files]", len(paths))
    subprocess.run(cmd, check=True)


def _gdalwarp_vrt(
    vrt_path: Path,
    out_path: Path,
    clip_bounds: tuple | None,
    target_res_m: float | None = None,
) -> None:
    """Warp a VRT to a Cloud Optimized GeoTIFF in EPSG:3857."""
    step(f"Warping {vrt_path.name} → {out_path.name} ...")
    tmp = out_path.with_suffix(".tmp.tif")
    cmd = [
        "gdalwarp",
        "-t_srs", OUTPUT_CRS,
        "-r", "bilinear",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=YES",
        "-co", "BLOCKXSIZE=512",
        "-co", "BLOCKYSIZE=512",
        "-wm", "2048",
        "-multi",
        "-overwrite",
    ]
    if target_res_m is not None:
        cmd += ["-tr", str(target_res_m), str(target_res_m)]
    if clip_bounds:
        w, s, e, n = clip_bounds
        cmd += ["-te", str(w), str(s), str(e), str(n), "-te_srs", "EPSG:4326"]
    cmd += [str(vrt_path), str(tmp)]
    logger.info("  %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    step("Building overviews ...")
    subprocess.run(["gdaladdo", "-r", "average", str(tmp),
                    "2", "4", "8", "16", "32", "64"], check=True)

    step(f"Finalizing COG → {out_path.name} ...")
    rio_copy(str(tmp), str(out_path),
             copy_src_overviews=True,
             driver="GTiff", tiled=True,
             blockxsize=512, blockysize=512,
             compress="deflate", BIGTIFF="YES")
    tmp.unlink(missing_ok=True)
    logger.info("  → %s  (%.0f MB)", out_path.name, out_path.stat().st_size / 1e6)


def _tiles_tight_bbox(paths: list[Path]) -> tuple[float, float, float, float]:
    """Return WGS84 (W, S, E, N) union bounding box of all tile files."""
    import math as _math
    from rasterio.warp import transform_bounds as _tb
    west = south = _math.inf
    east = north = -_math.inf
    for p in paths:
        with rasterio.open(p) as ds:
            b = ds.bounds
            if ds.crs.to_epsg() != 4326:
                b = _tb(ds.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
            west  = min(west,  b[0])
            south = min(south, b[1])
            east  = max(east,  b[2])
            north = max(north, b[3])
    return (west, south, east, north)


def _estimate_raster_gb(west: float, south: float, east: float, north: float,
                        res_m: float = 1.0) -> float:
    """Rough estimate of uncompressed float32 raster size in GB for a WGS84 bbox."""
    import math as _math
    lat_c     = _math.radians((north + south) / 2)
    width_m   = (east - west)  * _math.cos(lat_c) * 111_320
    height_m  = (north - south) * 111_320
    return max(width_m, 0) * max(height_m, 0) / (res_m ** 2) * 4 / 1e9


# ── derivatives ────────────────────────────────────────────────────────────────

def compute_derivatives_windowed(
    dem_path: Path,
    cog_dir: Path,
    res_x: float,
    res_y: float,
    nodata=None,
    strip_height: int = 2048,
    overlap: int = 32,
    azimuth: float = 315.0,
    altitude: float = 45.0,
    suffix: str = "",
) -> tuple[Path, Path, Path]:
    """
    Compute hillshade, slope, and aspect in horizontal strips so peak RAM
    stays manageable regardless of DEM size.

    Each strip is padded with `overlap` rows above and below for accurate
    np.gradient values at boundaries.  Only interior rows are written.

    suffix: appended before .tif in output filenames — use "_hires" for the
    1m priority-stack outputs so they don't collide with the 10m baseline.
    """
    step(f"Computing hillshade / slope / aspect{' (hires)' if suffix else ''} (windowed) ...")

    hs_path     = cog_dir / f"hillshade{suffix}.tif"
    slope_path  = cog_dir / f"slope{suffix}.tif"
    aspect_path = cog_dir / f"aspect{suffix}.tif"

    sun_zenith  = float(np.radians(90.0 - altitude))
    sun_azimuth = float(np.radians(360.0 - azimuth + 90.0))

    common = dict(driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
                  compress="deflate", BIGTIFF="YES")
    hs_tmp     = str(hs_path)     + ".tmp.tif"
    slope_tmp  = str(slope_path)  + ".tmp.tif"
    aspect_tmp = str(aspect_path) + ".tmp.tif"

    with rasterio.open(dem_path) as dem_ds:
        height = dem_ds.height
        width  = dem_ds.width
        nd     = nodata if nodata is not None else dem_ds.nodata
        base   = dict(width=width, height=height, count=1,
                      crs=dem_ds.crs, transform=dem_ds.transform)

        hs_prof    = {**base, "dtype": "uint8",   "nodata": 0}
        slope_prof = {**base, "dtype": "float32", "nodata": -9999.0}
        asp_prof   = {**base, "dtype": "float32", "nodata": -9999.0}

        n_strips = (height + strip_height - 1) // strip_height
        # RAM hint: one strip (with overlap, as float32)
        strip_mb = (strip_height + 2 * overlap) * width * 4 / 1e6
        logger.info("  DEM %d × %d  strip_height=%d  %d strips  ~%.0f MB RAM/strip",
                    width, height, strip_height, n_strips, strip_mb)

        with (
            rasterio.open(hs_tmp,     "w", **{**hs_prof,    **common}) as hs_ds,
            rasterio.open(slope_tmp,  "w", **{**slope_prof, **common}) as sl_ds,
            rasterio.open(aspect_tmp, "w", **{**asp_prof,   **common}) as as_ds,
        ):
            t_run = time.time()
            for i in range(n_strips):
                t_strip = time.time()
                r_start  = i * strip_height
                r_end    = min(r_start + strip_height, height)

                read_start = max(0, r_start - overlap)
                read_end   = min(height, r_end + overlap)
                top_pad    = r_start - read_start

                win = Window(col_off=0, row_off=read_start,
                             width=width, height=read_end - read_start)
                d = dem_ds.read(1, window=win).astype(np.float32)

                if nd is not None:
                    d[d == nd] = np.nan

                dy_arr, dx = np.gradient(d, res_y, res_x)
                dy_arr *= -1
                del d

                s    = top_pad
                e_r  = s + (r_end - r_start)
                dx_i  = dx[s:e_r].copy()
                dy_i  = dy_arr[s:e_r].copy()
                del dx, dy_arr

                mag       = np.sqrt(dx_i**2 + dy_i**2)
                slope_rad = np.arctan(mag)
                del mag
                aspect_rad = np.arctan2(dy_i, dx_i)
                del dx_i, dy_i

                shade = (
                    np.cos(sun_zenith) * np.cos(slope_rad)
                    + np.sin(sun_zenith) * np.sin(slope_rad) * np.cos(sun_azimuth - aspect_rad)
                )
                np.clip(shade, 0.0, 1.0, out=shade)
                shade *= 255.0
                np.nan_to_num(shade, nan=0.0, copy=False)
                hs_strip = shade.astype(np.uint8)
                del shade

                slope_deg = np.degrees(slope_rad)
                del slope_rad
                np.nan_to_num(slope_deg, nan=-9999.0, copy=False)

                aspect_deg = np.degrees(aspect_rad) % 360.0
                del aspect_rad
                np.nan_to_num(aspect_deg, nan=-9999.0, copy=False)

                out_win = Window(col_off=0, row_off=r_start,
                                 width=width, height=r_end - r_start)
                hs_ds.write(hs_strip[np.newaxis],                      window=out_win)
                sl_ds.write(slope_deg[np.newaxis].astype(np.float32),  window=out_win)
                as_ds.write(aspect_deg[np.newaxis].astype(np.float32), window=out_win)
                del hs_strip, slope_deg, aspect_deg

                # Progress with elapsed and ETA
                elapsed_total = time.time() - t_run
                strip_s       = time.time() - t_strip
                done_frac     = (i + 1) / n_strips
                eta_s         = elapsed_total / done_frac - elapsed_total if done_frac > 0 else 0
                logger.info("  strip %d/%d  rows %d–%d  %.1fs/strip  elapsed %s  ETA %s",
                            i + 1, n_strips, r_start, r_end - 1, strip_s,
                            _fmt_duration(elapsed_total), _fmt_duration(eta_s))

    # Build overviews and finalise each file as a proper COG.
    for tmp, out in [(hs_tmp, hs_path), (slope_tmp, slope_path), (aspect_tmp, aspect_path)]:
        step(f"Finalizing {out.name} ...")
        with rasterio.open(tmp, "r+") as ds:
            ds.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
        rio_copy(tmp, str(out), copy_src_overviews=True,
                 driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
                 compress="deflate", BIGTIFF="YES")
        os.remove(tmp)
        logger.info("  → %s  (%.0f MB)", out.name, out.stat().st_size / 1e6)

    return hs_path, slope_path, aspect_path


def _fmt_duration(seconds: float) -> str:
    """Format seconds → 'HH:MM:SS'."""
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ── COG write ──────────────────────────────────────────────────────────────────

def write_cog(arr: np.ndarray, profile: dict, out_path: Path) -> None:
    """Write arr to a Cloud Optimized GeoTIFF at out_path (via tmp file)."""
    tmp = str(out_path) + ".tmp.tif"
    p = {k: v for k, v in profile.items() if k != "copy_src_overviews"}
    p.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
             compress="deflate", BIGTIFF="YES")
    with rasterio.open(tmp, "w", **p) as ds:
        ds.write(arr)
        ds.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
        ds.update_tags(ns="rio_overview", resampling="average")
    rio_copy(tmp, str(out_path), copy_src_overviews=True,
             driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
             compress="deflate", BIGTIFF="YES")
    os.remove(tmp)


# ── upload ─────────────────────────────────────────────────────────────────────

class _ProgressCallback:
    """boto3 callback that logs upload progress every 10%."""
    def __init__(self, total_bytes: int, label: str):
        self._total   = total_bytes
        self._label   = label
        self._seen    = 0
        self._last_pct = -10

    def __call__(self, bytes_amount: int) -> None:
        self._seen += bytes_amount
        pct = 100 * self._seen // self._total if self._total else 100
        if pct >= self._last_pct + 10:
            self._last_pct = pct
            logger.info("  %s  %d%%  (%.0f / %.0f MB)",
                        self._label, pct,
                        self._seen / 1e6, self._total / 1e6)


def upload_cogs(cog_files: list[tuple[str, Path]],
                endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
    """Upload (s3_key, local_path) pairs to MinIO with progress logging."""
    step(f"Uploading {len(cog_files)} COGs → s3://{bucket}/")
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )
    for key, local in cog_files:
        size = local.stat().st_size
        logger.info("  uploading %s  (%.0f MB)", local.name, size / 1e6)
        cb = _ProgressCallback(size, local.name)
        s3.upload_file(str(local), bucket, key, Callback=cb)
        logger.info("  ✓  s3://%s/%s", bucket, key)


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    parser = argparse.ArgumentParser(
        description="Whumpf DEM pipeline — downloads 3DEP, computes derivatives, uploads COGs"
    )
    parser.add_argument("--bbox", default=",".join(str(x) for x in DEFAULT_BBOX),
                        help="W,S,E,N  (default: San Juan Mountains)")
    parser.add_argument("--resolution", choices=["1/3", "1m"], default="1/3",
                        help="DEM resolution: '1/3' = 10m (default), '1m' = 1-meter lidar")
    parser.add_argument("--region", default="sanjuans",
                        help="MinIO prefix / region label  (default: sanjuans)")
    parser.add_argument("--env", default=str(repo_root / ".env"),
                        help="Path to .env file")
    parser.add_argument("--workdir", default=None,
                        help="Working directory (default: system temp)")
    parser.add_argument("--strip-height", type=int, default=None,
                        help="Derivative strip height in rows "
                             "(default: 2048 for 10m, 512 for 1m)")
    parser.add_argument("--use-gdalwarp", action="store_true",
                        help="Force gdalbuildvrt + gdalwarp for mosaic (streaming, low RAM). "
                             "Automatically set for --resolution 1m. Use for large 10m bboxes "
                             "where in-memory merge would exceed available RAM.")
    parser.add_argument("--priority-stack", action="store_true",
                        help="Download both 10m (1/3 arc-second) and 1m lidar tiles. "
                             "Blend into dem.tif at 10m using 1m data where available (1m wins). "
                             "Also produces dem_hires.tif at native 1m if the estimated "
                             "uncompressed size is under --hires-max-gb. Implies --use-gdalwarp.")
    parser.add_argument("--hires-max-gb", type=float, default=HIRES_MAX_UNCOMPRESSED_GB,
                        help=f"Skip hires COG if estimated uncompressed size exceeds this GB "
                             f"(default: {HIRES_MAX_UNCOMPRESSED_GB}). Increase for smaller "
                             "focused regions where 1m coverage is dense.")
    parser.add_argument("--hires-bbox", default=None, metavar="W,S,E,N",
                        help="Restrict the 1m hires COG to this sub-bbox (WGS84). "
                             "Useful for limiting hires output to mountain terrain while "
                             "keeping the full bbox for the 10m dem.tif baseline. "
                             "Defaults to the same bbox as --bbox.")
    parser.add_argument("--skip-hires-upload", action="store_true",
                        help="Skip uploading dem_hires.tif to MinIO (161 GB+). "
                             "Derivatives (slope_hires, hillshade_hires, aspect_hires) "
                             "are still uploaded. Use when disk is tight and "
                             "terrain_rgb_pipeline will read dem_hires.tif locally.")
    parser.add_argument("--test", action="store_true",
                        help="Smoke-test: small bbox near Silverton (~2 min)")
    args = parser.parse_args()

    # Working directory — persistent across runs so downloads are resumable.
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = Path(tempfile.mkdtemp(prefix="whumpf-dem-"))

    setup_logging(workdir / "pipeline.log")
    logger.info("=" * 60)
    logger.info("Whumpf DEM pipeline  resolution=%s  region=%s",
                args.resolution, args.region)
    logger.info("Working directory: %s", workdir)

    # --test overrides bbox
    if args.test:
        bbox = (-108.0, 37.7, -107.5, 38.2)
        logger.info("--test mode: bbox overridden to %s", bbox)
    else:
        try:
            parts = [float(x) for x in args.bbox.split(",")]
            assert len(parts) == 4
            bbox = tuple(parts)
        except Exception:
            parser.error("--bbox must be W,S,E,N (four floats)")

    # Hires bbox — defaults to full bbox when not specified
    if args.hires_bbox:
        try:
            hparts = [float(x) for x in args.hires_bbox.split(",")]
            assert len(hparts) == 4
            hires_bbox = tuple(hparts)
        except Exception:
            parser.error("--hires-bbox must be W,S,E,N (four floats)")
    else:
        hires_bbox = bbox

    # Strip height defaults
    strip_height = args.strip_height or (512 if args.resolution == "1m" else 2048)

    # gdalwarp: mandatory for 1m, priority-stack, or large 10m bboxes
    use_gdalwarp = args.resolution == "1m" or args.use_gdalwarp or args.priority_stack
    if use_gdalwarp and not check_gdalwarp():
        sys.exit(
            "ERROR: gdalwarp and gdalbuildvrt are required for "
            "--use-gdalwarp / --resolution 1m / --priority-stack.\n"
            "Install with:  sudo apt-get install -y gdal-bin"
        )

    # Disk space estimates (conservative)
    if args.resolution == "1m":
        need_gb = 150.0
    elif args.priority_stack:
        need_gb = 80.0   # 10m baseline + 1m tile downloads + hires COG + derivatives
    elif use_gdalwarp:
        need_gb = 30.0
    else:
        need_gb = 5.0
    check_disk_space(workdir, need_gb)

    # Credentials
    env = load_env(args.env)
    minio_user = env.get("MINIO_ROOT_USER", "")
    minio_pass = env.get("MINIO_ROOT_PASSWORD", "")
    minio_ep   = env.get("MINIO_ENDPOINT", "http://localhost:9000")
    if not minio_user or not minio_pass:
        sys.exit(f"ERROR: MINIO_ROOT_USER / MINIO_ROOT_PASSWORD not set in {args.env}")

    raw_dir = workdir / "raw"
    cog_dir = workdir / "cog"
    raw_dir.mkdir(exist_ok=True)
    cog_dir.mkdir(exist_ok=True)

    t_start = time.time()

    dem_path       = cog_dir / "dem.tif"
    dem_hires_path = cog_dir / "dem_hires.tif"
    clip_bounds    = (bbox[0], bbox[1], bbox[2], bbox[3])

    # ── 1 + 2: download + mosaic → dem.tif ───────────────────────────────────

    if args.priority_stack:
        # ── priority stack: 10m baseline blended with 1m where available ─────
        raw_10m = raw_dir / "10m"
        raw_1m  = raw_dir / "1m"
        raw_10m.mkdir(exist_ok=True)
        raw_1m.mkdir(exist_ok=True)

        paths_10m: list[Path] = []
        paths_1m:  list[Path] = []

        if dem_path.exists():
            step("dem.tif already exists — skipping download and priority-blend mosaic.")
        else:
            # Download 10m baseline
            tiles_10m = query_tnm(bbox, resolution="1/3")
            if not tiles_10m:
                sys.exit("ERROR: TNM returned zero 10m tiles for this bbox.")
            paths_10m = download_tiles(tiles_10m, raw_10m)

            # Download 1m lidar (if any tiles exist for this bbox)
            step("Querying 1m lidar tiles ...")
            tiles_1m = query_tnm(bbox, resolution="1m")
            if tiles_1m:
                logger.info("  %d 1m tile(s) available — downloading ...", len(tiles_1m))
                paths_1m = download_tiles(tiles_1m, raw_1m)
            else:
                logger.info("  No 1m lidar tiles found for this bbox — 10m-only baseline.")

            # Priority VRT: 10m listed first, 1m listed last → 1m wins on overlap.
            vrt = cog_dir / "priority.vrt"
            _build_vrt(paths_10m + paths_1m, vrt)
            _gdalwarp_vrt(vrt, dem_path, clip_bounds,
                          target_res_m=PRIORITY_STACK_RES_M)
            vrt.unlink(missing_ok=True)

        # ── hires COG: 1m-only, clipped to actual tile coverage ───────────────
        if not dem_hires_path.exists():
            if not paths_1m:
                # On a resumed run, discover already-downloaded 1m tiles.
                paths_1m = sorted(raw_1m.glob("*.tif"))

            if paths_1m:
                # Clip the tight bbox to hires_bbox so eastern plains don't inflate estimate.
                raw_tight = _tiles_tight_bbox(paths_1m)
                tight = (
                    max(raw_tight[0], hires_bbox[0]),
                    max(raw_tight[1], hires_bbox[1]),
                    min(raw_tight[2], hires_bbox[2]),
                    min(raw_tight[3], hires_bbox[3]),
                )
                est_gb  = _estimate_raster_gb(*tight, res_m=1.0)
                logger.info(
                    "  1m hires bbox W=%.3f S=%.3f E=%.3f N=%.3f  "
                    "estimated uncompressed %.1f GB",
                    *tight, est_gb,
                )
                if est_gb > args.hires_max_gb:
                    logger.warning(
                        "  Skipping dem_hires.tif — estimated %.1f GB exceeds "
                        "--hires-max-gb %.0f.  Use --hires-bbox W,S,E,N to restrict "
                        "to a smaller mountain sub-region.",
                        est_gb, args.hires_max_gb,
                    )
                else:
                    vrt_h = cog_dir / "hires.vrt"
                    # Pass hires_bbox so gdalbuildvrt only includes intersecting tiles.
                    _build_vrt(paths_1m, vrt_h, clip_bounds=hires_bbox)
                    _gdalwarp_vrt(vrt_h, dem_hires_path, tight)
                    vrt_h.unlink(missing_ok=True)
            else:
                logger.info("  No 1m tiles found — skipping dem_hires.tif.")
        else:
            step("dem_hires.tif already exists — skipping hires mosaic.")

        # Read dem.tif metadata after the priority stack block.
        with rasterio.open(dem_path) as _ds:
            dst_tf = _ds.transform
            res_x  = abs(dst_tf.a)
            res_y  = abs(dst_tf.e)
            nodata = _ds.nodata

    else:
        # ── standard single-resolution path (unchanged) ───────────────────────
        if dem_path.exists():
            step("dem.tif already exists — skipping download and mosaic.")
            tile_paths: list[Path] = []
        else:
            tiles = query_tnm(bbox, resolution=args.resolution)
            if not tiles:
                sys.exit(
                    f"ERROR: TNM returned zero tiles for bbox={bbox} "
                    f"resolution={args.resolution}.\n"
                    "Browse https://apps.nationalmap.gov/downloader/ to verify coverage."
                )
            tile_paths = download_tiles(tiles, raw_dir)

        _, base_profile, dst_tf = mosaic_and_reproject(
            tile_paths, dem_path,
            clip_bounds=clip_bounds,
            use_gdalwarp=use_gdalwarp,
        )
        res_x  = abs(dst_tf.a)
        res_y  = abs(dst_tf.e)
        nodata = base_profile.get("nodata")

    # ── 3–5. hillshade / slope / aspect (10m baseline) ────────────────────────
    hs_path     = cog_dir / "hillshade.tif"
    slope_path  = cog_dir / "slope.tif"
    aspect_path = cog_dir / "aspect.tif"

    if hs_path.exists() and slope_path.exists() and aspect_path.exists():
        step("Derivative COGs already exist — skipping computation.")
    else:
        compute_derivatives_windowed(
            dem_path, cog_dir, res_x, res_y,
            nodata=nodata, strip_height=strip_height,
        )

    # ── 3b. hires derivatives (priority stack, when dem_hires.tif was produced) ─
    hs_hires_path     = cog_dir / "hillshade_hires.tif"
    slope_hires_path  = cog_dir / "slope_hires.tif"
    aspect_hires_path = cog_dir / "aspect_hires.tif"

    if dem_hires_path.exists():
        if hs_hires_path.exists() and slope_hires_path.exists() and aspect_hires_path.exists():
            step("Hires derivative COGs already exist — skipping computation.")
        else:
            with rasterio.open(dem_hires_path) as _ds:
                hires_tf    = _ds.transform
                hires_res_x = abs(hires_tf.a)
                hires_res_y = abs(hires_tf.e)
                hires_nodata = _ds.nodata
            compute_derivatives_windowed(
                dem_hires_path, cog_dir,
                hires_res_x, hires_res_y,
                nodata=hires_nodata, strip_height=512,
                suffix="_hires",
            )

    # ── 6. upload ─────────────────────────────────────────────────────────────
    prefix = args.region.rstrip("/")
    cog_files: list[tuple[str, Path]] = [
        (f"{prefix}/dem.tif",       dem_path),
        (f"{prefix}/hillshade.tif", hs_path),
        (f"{prefix}/slope.tif",     slope_path),
        (f"{prefix}/aspect.tif",    aspect_path),
    ]
    if dem_hires_path.exists():
        hires_uploads: list[tuple[str, Path]] = []
        if not args.skip_hires_upload:
            hires_uploads.append((f"{prefix}/dem_hires.tif", dem_hires_path))
        else:
            logger.info("  --skip-hires-upload: skipping dem_hires.tif (%.0f GB)",
                        dem_hires_path.stat().st_size / 1e9)
        hires_uploads += [
            (f"{prefix}/hillshade_hires.tif", hs_hires_path),
            (f"{prefix}/slope_hires.tif",     slope_hires_path),
            (f"{prefix}/aspect_hires.tif",    aspect_hires_path),
        ]
        cog_files += hires_uploads
    upload_cogs(
        cog_files,
        endpoint=minio_ep,
        access_key=minio_user,
        secret_key=minio_pass,
        bucket=MINIO_BUCKET,
    )

    elapsed = _fmt_duration(time.time() - t_start)
    step(f"Pipeline complete.  Total time: {elapsed}")

    base    = f"http://localhost:9000/{MINIO_BUCKET}/{prefix}"
    titiler = "http://localhost:8001"
    hires_lines = ""
    if dem_hires_path.exists():
        hires_lines = (
            f"\n    Hillshade (hires): {titiler}/cog/viewer?url={base}/hillshade_hires.tif"
            f"\n    Slope (hires):     {titiler}/cog/preview.png?url={base}/slope_hires.tif"
            f"&colormap_name=rdylgn_r&rescale=0,60&nodata=-9999"
            f"\n    DEM hires info:    {titiler}/cog/info?url={base}/dem_hires.tif"
        )
    print(f"""
  Visual checks (open in browser — use VM LAN IP if remote):

    Hillshade:  {titiler}/cog/viewer?url={base}/hillshade.tif
    Slope:      {titiler}/cog/preview.png?url={base}/slope.tif&colormap_name=rdylgn_r&rescale=0,60&nodata=-9999
    Aspect:     {titiler}/cog/preview.png?url={base}/aspect.tif&colormap_name=hsv&rescale=0,360&nodata=-9999
    DEM info:   {titiler}/cog/info?url={base}/dem.tif{hires_lines}

  Log file:  {workdir / "pipeline.log"}
  Raw tiles: {raw_dir}
""")


if __name__ == "__main__":
    main()
