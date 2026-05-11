"""Region registry — single source of truth for what `?region=` values mean.

Each Region carries the bbox/center/mask metadata that the frontend needs to
configure MapLibre, plus the slug used in S3 paths (`dem-cogs/<id>/dem.tif`)
and tile URLs. Add a new region by appending it to REGIONS and dropping the
matching COGs in MinIO under the same id.

The validate_region helper enforces the registry at every `?region=` query
parameter — silently accepting arbitrary strings would let a caller probe
S3 bucket contents and bypass per-region cost controls as we expand.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel


class Region(BaseModel):
    id: str
    label: str
    # lon_min, lat_min, lon_max, lat_max — content extent for raster sources.
    bbox: tuple[float, float, float, float]
    # Padded extent used as the MapLibre map maxBounds when the region lock
    # is on. Slightly larger than bbox so map controls aren't right at the edge.
    max_bounds: tuple[float, float, float, float]
    # Default fly-to (lon, lat).
    center: tuple[float, float]
    default_zoom: int
    # GeoJSON FeatureCollection with the world as the outer ring and the
    # region as a hole — renders as a black fill masking outside-region.
    mask_geojson: dict[str, Any]


COLORADO = Region(
    id="colorado",
    label="Colorado",
    bbox=(-109.06, 37.0, -104.5, 41.0),
    max_bounds=(-109.5, 36.5, -101.5, 41.5),
    center=(-105.5, 39.0),
    default_zoom=7,
    mask_geojson={
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]],
                    [
                        [-109.25, 36.80], [-101.85, 36.80],
                        [-101.85, 41.20], [-109.25, 41.20],
                        [-109.25, 36.80],
                    ],
                ],
            },
            "properties": {},
        }],
    },
)


REGIONS: dict[str, Region] = {
    "colorado": COLORADO,
}


def validate_region(region: str) -> str:
    """Raise 400 if `region` isn't in the registry. Returns the validated region."""
    if region not in REGIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown region: {region}")
    return region
