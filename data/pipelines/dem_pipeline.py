#!/usr/bin/env python3
"""
DEM pipeline for Whumpf — Phase 2.

Downloads USGS 3DEP 1/3 arc-second DEMs for the San Juan Mountains (or any
bbox), mosaics and reprojects to EPSG:3857, computes hillshade / slope /
aspect, converts all four outputs to Cloud Optimized GeoTIFF, and uploads to
MinIO dem-cogs.

Usage:
    python dem_pipeline.py [--bbox W,S,E,N] [--region PREFIX] [--workdir DIR]

Deps (install into a local venv first):
    pip install -r data/pipelines/requirements.txt
    # or:  pip install rasterio numpy boto3 httpx
"""

import argparse
import contextlib
import gc
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    import httpx
    import numpy as np
    import rasterio
    from rasterio import crs as rcrs
    from rasterio.enums import Resampling
    from rasterio.merge import merge as rio_merge
    from rasterio.shutil import copy as rio_copy
    from rasterio import warp as rwarp
    from rasterio.windows import Window
    import boto3
    from botocore.client import Config
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    print("Run:  pip install rasterio numpy boto3 httpx")
    sys.exit(1)

# ── constants ─────────────────────────────────────────────────────────────────

DEFAULT_BBOX   = (-108.5, 37.0, -106.5, 38.5)   # San Juan Mountains, CO
TNM_API        = "https://tnmaccess.nationalmap.gov/api/v1/products"
# Tag covering all NED/3DEP resolutions; prodFormats=GeoTIFF (case-sensitive) filters to rasters.
# We then pick only "1/3 Arc Second" titles and deduplicate to newest per degree cell.
TNM_DATASET    = "National Elevation Dataset (NED)"
MINIO_ENDPOINT = "http://localhost:9000"
MINIO_BUCKET   = "dem-cogs"
OUTPUT_CRS     = "EPSG:3857"

# ── helpers ───────────────────────────────────────────────────────────────────

def step(msg: str) -> None:
    print(f"\n[DEM] {msg}", flush=True)


def load_env(path: str) -> dict:
    """Parse a .env file; values in environment take precedence."""
    env: dict = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    # env-vars override file values
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith("MINIO_")})
    return env


# ── pipeline steps ────────────────────────────────────────────────────────────

def query_tnm(bbox: tuple, dataset: str = TNM_DATASET) -> list[dict]:
    """
    Return one {title, url} dict per 1°×1° degree cell covering bbox.

    TNM notes:
    - prodFormats must be "GeoTIFF" (exact case) — "GeoTiff" silently returns 0.
    - The dataset tag "National Elevation Dataset (NED)" covers all resolutions;
      we filter to titles containing "1/3 Arc Second" in post-processing.
    - Multiple historical versions exist per cell; we keep the newest (by date
      in the title, e.g. "… n37w107 20220801").
    """
    import re
    step(f"Querying TNM API  bbox={bbox}  dataset='{dataset}' ...")
    w, s, e, n = bbox
    params = {
        "datasets": dataset,
        "bbox": f"{w},{s},{e},{n}",
        "prodFormats": "GeoTIFF",   # capital TIFF — API is case-sensitive
        "outputFormat": "JSON",
        "max": 100,
        "offset": 0,
    }
    raw: list[dict] = []
    while True:
        for attempt in range(5):
            try:
                resp = httpx.get(TNM_API, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                wait = 2 ** attempt
                print(f"  TNM request failed (attempt {attempt + 1}/5): {exc!r} — retrying in {wait}s")
                if attempt == 4:
                    raise RuntimeError(f"TNM API failed after 5 attempts: {exc}") from exc
                time.sleep(wait)
        items = data.get("items", [])
        total = data.get("total", 0)
        raw.extend(items)
        print(f"  fetched {len(raw)}/{total}", flush=True)
        if len(raw) >= total or not items:
            break
        params["offset"] = len(raw)

    # Keep only 1/3 arc-second GeoTIFF items
    thirds = [
        it for it in raw
        if "1/3 Arc Second" in it.get("title", "")
        and it.get("downloadURL", "").lower().endswith((".tif", ".tiff"))
    ]

    # Deduplicate: one tile per degree cell, newest date wins
    best: dict[str, tuple[str, dict]] = {}
    for it in thirds:
        m = re.search(r"(n\d+w\d+)", it["title"])
        cell = m.group(1) if m else it["title"]
        m2 = re.search(r"(\d{8})\s*$", it["title"].strip())
        date = m2.group(1) if m2 else "00000000"
        if cell not in best or date > best[cell][0]:
            best[cell] = (date, it)

    result = [
        {"title": it["title"], "url": it["downloadURL"]}
        for it in sorted(
            (v for _, v in best.values()), key=lambda x: x["title"]
        )
    ]
    print(f"  {len(result)} tiles to download (1/3 arc-second, newest per cell)")
    return result


def download_tiles(tiles: list[dict], dest: Path) -> list[Path]:
    """Download each tile; skip if already present. Returns local paths."""
    step(f"Downloading {len(tiles)} tiles → {dest}")
    paths: list[Path] = []
    for i, tile in enumerate(tiles, 1):
        url = tile["url"]
        fname = url.split("/")[-1].split("?")[0] or f"tile_{i}.tif"
        out = dest / fname
        if out.exists():
            print(f"  [{i}/{len(tiles)}] skip (cached)  {fname}")
        else:
            print(f"  [{i}/{len(tiles)}] {fname} ...", end=" ", flush=True)
            with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
                r.raise_for_status()
                with out.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=131072):
                        f.write(chunk)
            mb = out.stat().st_size / 1_000_000
            print(f"{mb:.1f} MB")
        paths.append(out)
    return paths


def mosaic_and_reproject(paths: list[Path], out_path: Path,
                         clip_bounds: tuple | None = None) -> tuple:
    """
    Merge tiles (optionally clipped to clip_bounds), reproject to EPSG:3857,
    write as COG.  Returns (None, profile, transform) — the array is NOT kept
    in memory; use compute_derivatives_windowed to read dem_path in strips.
    Skips the heavy computation if out_path already exists.

    clip_bounds: (W, S, E, N) in source CRS (geographic degrees).
    """
    if out_path.exists():
        step(f"DEM COG exists, reading metadata from {out_path.name} ...")
        with rasterio.open(out_path) as ds:
            dst_tf = ds.transform
            profile = {
                "driver": "GTiff",
                "dtype": ds.dtypes[0],
                "width": ds.width,
                "height": ds.height,
                "count": 1,
                "crs": ds.crs,
                "transform": dst_tf,
                "nodata": ds.nodata,
            }
        print(f"  {profile['width']}×{profile['height']}  nodata={profile['nodata']}")
        return None, profile, dst_tf

    step(f"Mosaicking {len(paths)} tiles ...")
    merge_kwargs: dict = {}
    if clip_bounds is not None:
        merge_kwargs["bounds"] = clip_bounds   # (left, bottom, right, top) = (W,S,E,N)
    with contextlib.ExitStack() as stack:
        srcs = [stack.enter_context(rasterio.open(p)) for p in paths]
        src_crs = srcs[0].crs
        src_nodata = srcs[0].nodata
        mosaic, out_tf = rio_merge(srcs, **merge_kwargs)
    print(f"  mosaic  shape={mosaic.shape}  crs={src_crs}  nodata={src_nodata}")

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
        source=mosaic,
        destination=dst,
        src_transform=out_tf,
        src_crs=src_crs,
        dst_transform=dst_tf,
        dst_crs=dst_crs,
        resampling=Resampling.bilinear,
        src_nodata=src_nodata,
        dst_nodata=src_nodata,
    )
    del mosaic
    profile = {
        "driver": "GTiff",
        "dtype": dst.dtype,
        "width": dst_w,
        "height": dst_h,
        "count": 1,
        "crs": dst_crs,
        "transform": dst_tf,
        "nodata": src_nodata,
    }
    print(f"  reprojected  shape={dst.shape}")
    write_cog(dst, profile, out_path)
    del dst
    gc.collect()
    print(f"  → {out_path.name}")
    return None, profile, dst_tf


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
) -> tuple[Path, Path, Path]:
    """
    Compute hillshade, slope, and aspect from dem_path in horizontal strips so
    peak RAM stays around 2-3 GB regardless of DEM size.

    Each strip is read with `overlap` rows of padding above and below for
    accurate np.gradient values at strip boundaries.  Only the interior
    (non-overlap) rows are written to output.
    """
    step("Computing hillshade / slope / aspect (windowed) ...")

    hs_path    = cog_dir / "hillshade.tif"
    slope_path = cog_dir / "slope.tif"
    aspect_path = cog_dir / "aspect.tif"

    sun_zenith  = float(np.radians(90.0 - altitude))
    sun_azimuth = float(np.radians(360.0 - azimuth + 90.0))  # geographic → math

    common = dict(driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
                  compress="deflate", BIGTIFF="YES")

    hs_tmp     = str(hs_path)    + ".tmp.tif"
    slope_tmp  = str(slope_path) + ".tmp.tif"
    aspect_tmp = str(aspect_path) + ".tmp.tif"

    with rasterio.open(dem_path) as dem_ds:
        height = dem_ds.height
        width  = dem_ds.width
        nd     = nodata if nodata is not None else dem_ds.nodata
        base   = dict(width=width, height=height, count=1, crs=dem_ds.crs,
                      transform=dem_ds.transform)

        hs_prof    = {**base, "dtype": "uint8",   "nodata": 0}
        slope_prof = {**base, "dtype": "float32", "nodata": -9999.0}
        asp_prof   = {**base, "dtype": "float32", "nodata": -9999.0}

        with (
            rasterio.open(hs_tmp,    "w", **{**hs_prof,    **common}) as hs_ds,
            rasterio.open(slope_tmp, "w", **{**slope_prof, **common}) as sl_ds,
            rasterio.open(aspect_tmp,"w", **{**asp_prof,   **common}) as as_ds,
        ):
            n_strips = (height + strip_height - 1) // strip_height
            for i in range(n_strips):
                r_start = i * strip_height
                r_end   = min(r_start + strip_height, height)

                read_start = max(0, r_start - overlap)
                read_end   = min(height, r_end + overlap)
                top_pad    = r_start - read_start   # rows of padding at top of read window

                win = Window(col_off=0, row_off=read_start,
                             width=width, height=read_end - read_start)
                d = dem_ds.read(1, window=win).astype(np.float32)

                if nd is not None:
                    d[d == nd] = np.nan

                # Gradient: rows increase southward; negate dy for geographic north
                dy_arr, dx = np.gradient(d, res_y, res_x)
                dy_arr *= -1
                del d

                s    = top_pad
                e_r  = s + (r_end - r_start)

                # Extract interior rows from gradient arrays (slices → new arrays)
                dx_i  = dx[s:e_r].copy()
                dy_i  = dy_arr[s:e_r].copy()
                del dx, dy_arr

                mag        = np.sqrt(dx_i**2 + dy_i**2)
                slope_rad  = np.arctan(mag)
                del mag
                aspect_rad = np.arctan2(dy_i, dx_i)
                del dx_i, dy_i

                # Hillshade
                shade = (
                    np.cos(sun_zenith) * np.cos(slope_rad)
                    + np.sin(sun_zenith) * np.sin(slope_rad) * np.cos(sun_azimuth - aspect_rad)
                )
                np.clip(shade, 0.0, 1.0, out=shade)
                shade *= 255.0
                np.nan_to_num(shade, nan=0.0, copy=False)
                hs_strip = shade.astype(np.uint8)
                del shade

                # Slope (degrees)
                slope_deg = np.degrees(slope_rad)
                del slope_rad
                np.nan_to_num(slope_deg, nan=-9999.0, copy=False)

                # Aspect (degrees, 0=north clockwise)
                aspect_deg = np.degrees(aspect_rad) % 360.0
                del aspect_rad
                np.nan_to_num(aspect_deg, nan=-9999.0, copy=False)

                out_win = Window(col_off=0, row_off=r_start,
                                 width=width, height=r_end - r_start)
                hs_ds.write(hs_strip[np.newaxis],                     window=out_win)
                sl_ds.write(slope_deg[np.newaxis].astype(np.float32), window=out_win)
                as_ds.write(aspect_deg[np.newaxis].astype(np.float32),window=out_win)

                del hs_strip, slope_deg, aspect_deg
                print(f"  strip {i+1}/{n_strips}  rows {r_start}–{r_end-1}", flush=True)

    # Build overviews and finalize each output as a COG
    for tmp, out in [(hs_tmp, hs_path), (slope_tmp, slope_path), (aspect_tmp, aspect_path)]:
        step(f"Finalizing {out.name} ...")
        with rasterio.open(tmp, "r+") as ds:
            ds.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
        rio_copy(tmp, str(out), copy_src_overviews=True,
                 driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
                 compress="deflate", BIGTIFF="YES")
        os.remove(tmp)

    return hs_path, slope_path, aspect_path


def write_cog(arr: np.ndarray, profile: dict, out_path: Path) -> None:
    """Write arr to a valid Cloud Optimized GeoTIFF at out_path."""
    tmp = str(out_path) + ".tmp.tif"
    p = {k: v for k, v in profile.items() if k != "copy_src_overviews"}
    p.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
             compress="deflate", BIGTIFF="YES")

    with rasterio.open(tmp, "w", **p) as ds:
        ds.write(arr)
        ds.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
        ds.update_tags(ns="rio_overview", resampling="average")

    # copy_src_overviews lays the overviews before image data — the COG contract
    rio_copy(
        tmp, str(out_path),
        copy_src_overviews=True,
        driver="GTiff",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        compress="deflate",
        BIGTIFF="YES",
    )
    os.remove(tmp)


def upload_cogs(cog_files: list[tuple[str, Path]], endpoint: str,
                access_key: str, secret_key: str, bucket: str) -> None:
    """Upload (s3_key, local_path) pairs to MinIO."""
    step(f"Uploading {len(cog_files)} COGs → s3://{bucket}/")
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},   # MinIO requires path-style, not virtual-hosted
        ),
        region_name="us-east-1",
    )
    for key, local in cog_files:
        mb = local.stat().st_size / 1_000_000
        print(f"  {local.name}  ({mb:.1f} MB)  → {key}", flush=True)
        s3.upload_file(str(local), bucket, key)
        print(f"  ✓ {key}")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    parser = argparse.ArgumentParser(
        description="Whumpf DEM pipeline — downloads 3DEP, computes derivatives, uploads COGs"
    )
    parser.add_argument(
        "--bbox",
        default=",".join(str(x) for x in DEFAULT_BBOX),
        help="Bounding box W,S,E,N  (default: San Juan Mountains, CO)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Quick smoke-test: override bbox to a 0.5°×0.5° area near Silverton, CO "
            "(1 tile download, ~5 400×5 400 px output, completes in ~2 min). "
            "Use with --workdir to keep the download cached between runs."
        ),
    )
    parser.add_argument(
        "--region",
        default="sanjuans",
        help="MinIO prefix / region label  (default: sanjuans)",
    )
    parser.add_argument(
        "--env",
        default=str(repo_root / ".env"),
        help="Path to .env file  (default: repo root .env)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Working directory for downloads and intermediate files (default: system temp)",
    )
    args = parser.parse_args()

    # --test overrides bbox to a single 0.5°×0.5° tile near Silverton, CO
    TEST_BBOX = (-108.0, 37.7, -107.5, 38.2)
    if args.test:
        bbox = TEST_BBOX
        print(f"[DEM] --test mode: bbox overridden to {bbox}")
    else:
        try:
            bbox = tuple(float(x) for x in args.bbox.split(","))
            assert len(bbox) == 4
        except Exception:
            parser.error("--bbox must be four comma-separated floats: W,S,E,N")

    # ── credentials ──────────────────────────────────────────────────────────
    env = load_env(args.env)
    minio_user = env.get("MINIO_ROOT_USER", "")
    minio_pass = env.get("MINIO_ROOT_PASSWORD", "")
    _placeholders = {"", "-", "change-me-to-something-strong"}
    if minio_user in _placeholders or minio_pass in _placeholders:
        sys.exit(
            "ERROR: MINIO_ROOT_USER / MINIO_ROOT_PASSWORD are empty.\n"
            f"Set them in {args.env} or as environment variables."
        )

    # ── working directories ───────────────────────────────────────────────────
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = Path(tempfile.mkdtemp(prefix="whumpf-dem-"))

    raw_dir = workdir / "raw"
    cog_dir = workdir / "cog"
    raw_dir.mkdir(exist_ok=True)
    cog_dir.mkdir(exist_ok=True)

    step(f"Working directory: {workdir}")

    # ── 1. query + download (skipped when dem.tif already exists) ─────────────
    dem_path = cog_dir / "dem.tif"
    if dem_path.exists():
        step("dem.tif found — skipping TNM query and tile download.")
        tile_paths = []
    else:
        tiles = query_tnm(bbox)
        if not tiles:
            sys.exit(
                "ERROR: TNM returned zero tiles.\n"
                f"Check bbox {bbox} and dataset name '{TNM_DATASET}'.\n"
                f"Browse: https://apps.nationalmap.gov/downloader/"
            )
        tile_paths = download_tiles(tiles, raw_dir)

    # ── 2. mosaic + reproject → DEM COG ──────────────────────────────────────
    # Pass bbox as clip_bounds so the merge output is trimmed to exactly the
    # requested area — critical for fast test runs with small bboxes.
    clip_bounds = (bbox[0], bbox[1], bbox[2], bbox[3])  # W, S, E, N
    _, base_profile, dst_tf = mosaic_and_reproject(tile_paths, dem_path,
                                                   clip_bounds=clip_bounds)

    res_x  = abs(dst_tf.a)   # column spacing (east)
    res_y  = abs(dst_tf.e)   # row spacing (south — already positive here)
    nodata = base_profile.get("nodata")

    # ── 3–5. hillshade / slope / aspect (windowed — no large arrays in RAM) ───
    hs_path    = cog_dir / "hillshade.tif"
    slope_path = cog_dir / "slope.tif"
    aspect_path = cog_dir / "aspect.tif"

    if hs_path.exists() and slope_path.exists() and aspect_path.exists():
        step("Derivative COGs exist — skipping hillshade/slope/aspect.")
    else:
        compute_derivatives_windowed(dem_path, cog_dir, res_x, res_y, nodata=nodata)

    # ── 6. upload ─────────────────────────────────────────────────────────────
    prefix = args.region.rstrip("/")
    upload_cogs(
        [
            (f"{prefix}/dem.tif",       dem_path),
            (f"{prefix}/hillshade.tif", hs_path),
            (f"{prefix}/slope.tif",     slope_path),
            (f"{prefix}/aspect.tif",    aspect_path),
        ],
        endpoint=MINIO_ENDPOINT,
        access_key=minio_user,
        secret_key=minio_pass,
        bucket=MINIO_BUCKET,
    )

    step("Pipeline complete.")
    # TiTiler fetches COGs from MinIO server-side (host networking → localhost works).
    # The viewer and preview endpoints are the easiest way to visually confirm output.
    base = f"http://localhost:9000/{MINIO_BUCKET}/{prefix}"
    titiler = "http://localhost:8001"
    print(f"""
  Visual checks — open in a browser (use the VM's LAN IP if browsing from another machine):

    Hillshade viewer (map):
      {titiler}/cog/viewer?url={base}/hillshade.tif

    Hillshade preview image:
      {titiler}/cog/preview.png?url={base}/hillshade.tif

    Slope preview (red=steep):
      {titiler}/cog/preview.png?url={base}/slope.tif&colormap_name=rdylgn_r&rescale=0,60&nodata=-9999

    Aspect preview (hue = compass direction):
      {titiler}/cog/preview.png?url={base}/aspect.tif&colormap_name=hsv&rescale=0,360&nodata=-9999

    COG metadata (JSON):
      {titiler}/cog/info?url={base}/dem.tif

  Raw files: {workdir}
""")


if __name__ == "__main__":
    main()
