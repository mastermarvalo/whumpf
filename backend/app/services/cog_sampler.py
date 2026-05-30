"""Sample terrain COG files along a line segment or multi-vertex polyline."""

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


def _sample_coords(
    coords_3857: list[tuple[float, float]],
    distances: list[float],
    region: str,
    settings: Settings,
) -> list[TerrainSample]:
    """Sample slope/dem/aspect COGs at the given Web-Mercator coords.

    `coords_3857` and `distances` are parallel lists (cumulative distance in
    metres along the path). Returns one TerrainSample per coord.
    """
    n = len(coords_3857)

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
                    raw = [float(v[0]) for v in ds.sample(coords_3857)]
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


def sample_profile(
    start: tuple[float, float],
    end: tuple[float, float],
    region: str,
    settings: Settings,
    n: int = 64,
) -> list[TerrainSample]:
    """Sample slope, elevation, and aspect along a single A→B line at n
    evenly-spaced points.

    start/end are (longitude, latitude) in WGS84. Thin wrapper over
    `sample_polyline` so the A→B measure tool and route create path share one
    sampling implementation.
    """
    return sample_polyline([start, end], region, settings, n_total=n)


def sample_polyline(
    vertices: list[tuple[float, float]],
    region: str,
    settings: Settings,
    n_total: int = 128,
) -> list[TerrainSample]:
    """Sample slope, elevation, and aspect along an ordered polyline.

    `vertices` is an ordered list of (longitude, latitude) in WGS84 with at
    least two entries. The polyline is resampled into ``n_total`` points spaced
    evenly by cumulative distance — independent of the vertex count — so a
    hand-drawn 3-point route and an imported Strava track with thousands of
    vertices both yield the same compact, length-weighted profile.
    """
    if len(vertices) < 2:
        raise ValueError("sample_polyline needs at least two vertices")
    n = max(2, int(n_total))

    # Cumulative geodetic distance at each vertex (breakpoints for interpolation).
    vdist = [0.0]
    for (lng0, lat0), (lng1, lat1) in zip(vertices, vertices[1:]):
        _, _, m = _geod.inv(lng0, lat0, lng1, lat1)
        vdist.append(vdist[-1] + m)
    total_m = vdist[-1]

    if total_m <= 0:
        # Degenerate (all vertices coincident) — sample the single point.
        lng0, lat0 = vertices[0]
        lngs = [lng0] * n
        lats = [lat0] * n
        dists = [0.0] * n
    else:
        targets = np.linspace(0.0, total_m, n)
        vlng = np.array([v[0] for v in vertices])
        vlat = np.array([v[1] for v in vertices])
        # Piecewise-linear interpolation in lng/lat against cumulative distance:
        # equivalent to per-leg linspace but with even global spacing.
        lngs = np.interp(targets, vdist, vlng).tolist()
        lats = np.interp(targets, vdist, vlat).tolist()
        dists = targets.tolist()

    xs, ys = _to_3857.transform(lngs, lats)
    coords = list(zip(list(xs), list(ys)))
    return _sample_coords(coords, dists, region, settings)
