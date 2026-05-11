"""Sample terrain COG files along a line segment."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import rasterio
from pyproj import Geod, Transformer

from app.config import Settings

_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_geod = Geod(ellps="WGS84")


@dataclass
class TerrainSample:
    distance_m: float
    elevation_m: float | None
    slope_deg: float | None
    aspect_deg: float | None


def sample_profile(
    start: tuple[float, float],
    end: tuple[float, float],
    region: str,
    settings: Settings,
    n: int = 64,
) -> list[TerrainSample]:
    """Sample slope, elevation, and aspect along a line at n evenly-spaced points.

    start/end are (longitude, latitude) in WGS84.
    """
    start_lng, start_lat = start
    end_lng, end_lat = end

    _, _, total_m = _geod.inv(start_lng, start_lat, end_lng, end_lat)
    distances = np.linspace(0.0, total_m, n)

    lngs = np.linspace(start_lng, end_lng, n)
    lats = np.linspace(start_lat, end_lat, n)
    xs, ys = _to_3857.transform(lngs, lats)
    coords = list(zip(xs.tolist(), ys.tolist()))

    # dem-cogs bucket has anonymous download, so we read via HTTP/vsicurl
    # rather than the S3 driver (avoids rasterio 1.4+ credential restrictions).
    base = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{region}"
    env_vars = dict(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_USE_HEAD="FALSE",
        GDAL_HTTP_MULTIPLEX="YES",
        GDAL_HTTP_VERSION="2",
    )

    def _sample_one(name: str) -> list[float | None]:
        """Sample a COG along coords. Prefer hires per-point, fall back to the
        base 10m COG where hires is nodata.

        Hires coverage is a strict subset of the region bbox (currently the
        western half of Colorado — Front Range, Colorado Springs, plains are
        outside). A point can be inside hires extent for some COGs and outside
        for others, so we always sample both and merge per-point.
        """
        hires_vals: list[float | None] = [None] * n
        base_vals: list[float | None] = [None] * n
        for suffix, target in (("_hires", hires_vals), ("", base_vals)):
            try:
                with rasterio.open(f"/vsicurl/{base}/{name}{suffix}.tif") as ds:
                    nd = ds.nodata
                    raw = [float(v[0]) for v in ds.sample(coords)]
                for i, v in enumerate(raw):
                    target[i] = None if (nd is not None and v == nd) else v
            except Exception:
                # Variant doesn't exist / read failed → leave target all-None.
                pass
        return [
            hires_vals[i] if hires_vals[i] is not None else base_vals[i]
            for i in range(n)
        ]

    with rasterio.Env(**env_vars):
        slopes  = _sample_one("slope")
        elevs   = _sample_one("dem")
        aspects = _sample_one("aspect")

    return [
        TerrainSample(
            distance_m=float(distances[i]),
            elevation_m=elevs[i],
            slope_deg=slopes[i],
            aspect_deg=aspects[i],
        )
        for i in range(n)
    ]
