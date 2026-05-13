#!/usr/bin/env python3
"""
terrain_rgb_pipeline.py — Convert DEM COGs to terrarium-RGB MBTiles and upload to MinIO.

Reads dem.tif / dem_hires.tif from MinIO bucket dem-cogs, generates terrarium-encoded
RGB MBTiles (elevation = R*256 + G + B/256 - 32768 metres), uploads to MinIO.

The encoding matches the on-the-fly renderer in /tiles/terrain_rgb exactly, so
pre-built tiles served via a static tile server (e.g. Martin) will be pixel-identical
to the API endpoint.

Usage:
    python terrain_rgb_pipeline.py [--region PREFIX] [--env FILE] [--workdir DIR]
                                   [--skip-hires] [--min-z N] [--max-z N]
                                   [--max-z-hires N] [--workers N]

Deps (all in the pipelines venv):
    pip install boto3 rasterio mercantile Pillow numpy
"""

import argparse
import io
import logging
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import boto3
    import mercantile
    import numpy as np
    import rasterio
    from botocore.client import Config
    from botocore.exceptions import ClientError
    from PIL import Image
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    print("Run:  pip install boto3 rasterio mercantile Pillow numpy")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("terrain_rgb")

DEFAULT_REGION = "colorado"
MINIO_BUCKET   = "dem-cogs"
_WM_CRS        = CRS.from_epsg(3857)
_TILE_PX       = 256


def step(msg: str) -> None:
    logger.info("── %s", msg)


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


class _ProgressCallback:
    def __init__(self, total_bytes: int, label: str):
        self._total    = total_bytes
        self._label    = label
        self._seen     = 0
        self._last_pct = -10

    def __call__(self, bytes_amount: int) -> None:
        self._seen += bytes_amount
        pct = 100 * self._seen // self._total if self._total else 100
        if pct >= self._last_pct + 10:
            self._last_pct = pct
            logger.info("  %s  %d%%  (%.0f / %.0f MB)",
                        self._label, pct, self._seen / 1e6, self._total / 1e6)


def upload_files(
    files: list[tuple[str, Path]],
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
) -> None:
    step(f"Uploading {len(files)} file(s) → s3://{bucket}/")
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )
    for key, local in files:
        size = local.stat().st_size
        logger.info("  uploading %s  (%.0f MB)", local.name, size / 1e6)
        s3.upload_file(str(local), bucket, key, Callback=_ProgressCallback(size, local.name))
        logger.info("  ✓  s3://%s/%s", bucket, key)


# ── tile rendering ──────────────────────────────────────────────────────────────

def _encode_terrarium(data: np.ndarray) -> bytes:
    """Encode a (256, 256) float32 elevation array as a terrarium RGB PNG.

    Terrarium decode: elevation = R * 256 + G + B / 256 - 32768
    """
    elev = np.where(np.isnan(data), 0.0, data).astype(np.float64)
    val  = np.clip(elev + 32768.0, 0.0, 65535.0)
    int_val = np.floor(val).astype(np.uint32)
    r = (int_val >> 8).astype(np.uint8)
    g = (int_val & 0xFF).astype(np.uint8)
    b = np.floor((val - np.floor(val)) * 256.0).astype(np.uint8)
    img = np.stack([r, g, b], axis=-1)
    buf = io.BytesIO()
    Image.fromarray(img, mode="RGB").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _render_tile(dem_path: str, tile: mercantile.Tile) -> bytes:
    """Read one tile window from a local DEM COG and encode as terrarium PNG."""
    bounds  = mercantile.xy_bounds(tile)
    dst_tf  = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top,
                          _TILE_PX, _TILE_PX)
    data    = np.full((_TILE_PX, _TILE_PX), np.nan, dtype=np.float32)
    try:
        with rasterio.open(dem_path) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=data,
                dst_transform=dst_tf,
                dst_crs=_WM_CRS,
                resampling=Resampling.bilinear,
                dst_nodata=np.nan,
            )
    except Exception as exc:
        logger.debug("tile z%d/%d/%d render failed: %s", tile.z, tile.x, tile.y, exc)
    return _encode_terrarium(data)


def build_terrain_rgb(
    dem_path: Path,
    out_path: Path,
    min_z: int,
    max_z: int,
    workers: int = 4,
) -> None:
    """Generate terrarium-RGB MBTiles from a local DEM COG."""
    step(f"Building {out_path.name}  (z{min_z}–{max_z})  dem={dem_path.name}")

    with rasterio.open(dem_path) as src:
        w4326, s4326, e4326, n4326 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    logger.info("  DEM bounds (WGS84): W=%.4f S=%.4f E=%.4f N=%.4f",
                w4326, s4326, e4326, n4326)

    conn = sqlite3.connect(str(out_path))
    cur  = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
        CREATE TABLE IF NOT EXISTS tiles (
            zoom_level INTEGER,
            tile_column INTEGER,
            tile_row INTEGER,
            tile_data BLOB,
            PRIMARY KEY (zoom_level, tile_column, tile_row)
        );
    """)
    cur.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("name",     out_path.stem),
        ("format",   "png"),
        ("minzoom",  str(min_z)),
        ("maxzoom",  str(max_z)),
        ("bounds",   f"{w4326},{s4326},{e4326},{n4326}"),
        ("encoding", "terrarium"),
    ])
    conn.commit()

    dem_str = str(dem_path)

    for z in range(min_z, max_z + 1):
        tiles = list(mercantile.tiles(w4326, s4326, e4326, n4326, zooms=z))
        step(f"  z{z}: {len(tiles)} tiles")

        def _job(tile: mercantile.Tile) -> tuple[mercantile.Tile, bytes]:
            return tile, _render_tile(dem_str, tile)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in as_completed(pool.submit(_job, t) for t in tiles):
                tile, png = future.result()
                tms_y = (1 << z) - 1 - tile.y  # TMS y-flip (MBTiles spec)
                cur.execute(
                    "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                    (z, tile.x, tms_y, png),
                )
                done += 1
                if done % 500 == 0:
                    conn.commit()
                    logger.info("    z%d  %d/%d tiles (%.0f%%)", z, done, len(tiles),
                                100 * done / len(tiles))

        conn.commit()
        logger.info("  z%d complete  (%d tiles)", z, len(tiles))

    conn.close()
    step(f"  ✓  {out_path.name}  ({out_path.stat().st_size / 1e6:.0f} MB)")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    parser = argparse.ArgumentParser(
        description="Convert DEM COGs to terrarium-RGB MBTiles and upload to MinIO."
    )
    parser.add_argument("--region",       default=DEFAULT_REGION,
                        help="MinIO region prefix  (default: colorado)")
    parser.add_argument("--env",          default=str(repo_root / ".env"),
                        help="Path to .env file  (default: <repo>/.env)")
    parser.add_argument("--workdir",      default=None,
                        help="Working directory  (default: system temp)")
    parser.add_argument("--skip-hires",   action="store_true",
                        help="Skip hires MBTiles even if dem_hires.tif exists in MinIO")
    parser.add_argument("--min-z",        type=int, default=5,
                        help="Min zoom  (default: 5)")
    parser.add_argument("--max-z",        type=int, default=14,
                        help="Max zoom for terrain_rgb.mbtiles  (default: 14)")
    parser.add_argument("--max-z-hires",  type=int, default=16,
                        help="Max zoom for terrain_rgb_hires.mbtiles  (default: 16)")
    parser.add_argument("--workers",      type=int, default=4,
                        help="Tile-render worker threads  (default: 4)")
    args = parser.parse_args()

    env        = load_env(args.env)
    minio_user = env.get("MINIO_ROOT_USER", "")
    minio_pass = env.get("MINIO_ROOT_PASSWORD", "")
    minio_ep   = env.get("MINIO_ENDPOINT", "http://localhost:9000")
    if not minio_user or not minio_pass:
        sys.exit(f"ERROR: MINIO_ROOT_USER / MINIO_ROOT_PASSWORD not set in {args.env}")

    prefix = args.region.rstrip("/")

    s3 = boto3.client(
        "s3",
        endpoint_url=minio_ep,
        aws_access_key_id=minio_user,
        aws_secret_access_key=minio_pass,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )

    tmp_ctx = tempfile.TemporaryDirectory() if args.workdir is None else None
    workdir = Path(tmp_ctx.name if tmp_ctx else args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        dem_local       = workdir / "dem.tif"
        dem_hires_local = workdir / "dem_hires.tif"

        step(f"Downloading s3://{MINIO_BUCKET}/{prefix}/dem.tif")
        s3.download_file(MINIO_BUCKET, f"{prefix}/dem.tif", str(dem_local))

        hires_available = False
        if not args.skip_hires:
            try:
                step(f"Downloading s3://{MINIO_BUCKET}/{prefix}/dem_hires.tif")
                s3.download_file(MINIO_BUCKET, f"{prefix}/dem_hires.tif", str(dem_hires_local))
                hires_available = True
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                    logger.info("  dem_hires.tif not in MinIO — skipping hires MBTiles")
                else:
                    raise

        rgb_path = workdir / "terrain_rgb.mbtiles"
        build_terrain_rgb(dem_local, rgb_path,
                          min_z=args.min_z, max_z=args.max_z, workers=args.workers)
        upload_list: list[tuple[str, Path]] = [(f"{prefix}/terrain_rgb.mbtiles", rgb_path)]

        if hires_available:
            rgb_hires_path = workdir / "terrain_rgb_hires.mbtiles"
            build_terrain_rgb(dem_hires_local, rgb_hires_path,
                              min_z=args.min_z, max_z=args.max_z_hires, workers=args.workers)
            upload_list.append((f"{prefix}/terrain_rgb_hires.mbtiles", rgb_hires_path))

        upload_files(upload_list, endpoint=minio_ep,
                     access_key=minio_user, secret_key=minio_pass, bucket=MINIO_BUCKET)
        step("Done.")

    finally:
        if tmp_ctx:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    main()
