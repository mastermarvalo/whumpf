"""Terrain analysis endpoints."""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from pyproj import Geod

from app.auth.dependencies import get_current_user
from app.config import Settings, get_settings, validate_region
from app.models.user import User
from app.services.cog_sampler import sample_profile

router = APIRouter(prefix="/terrain", tags=["terrain"])
logger = logging.getLogger("whumpf.terrain")

# Cap a single profile request to a reasonable backcountry-route distance.
# Anything longer is almost certainly a bug or abuse — sampling a continental-scale
# line costs egress for tiny per-pixel value and provides no real-world utility.
_MAX_PROFILE_DISTANCE_M = 200_000  # 200 km

_GEOD = Geod(ellps="WGS84")

# Avalanche-terrain buckets matching CAIC's common terms — "low angle" is
# generally <30°, "danger zone" is 30–45°, ">45°" is steep enough that snow
# tends not to stick (mostly). The "27" bucket is where storm-slab triggering
# becomes plausible on a soft slab; surface as its own row so users can see it.
_SLOPE_BUCKETS: list[tuple[str, float, float]] = [
    ("0-15",  0.0, 15.0),
    ("15-27", 15.0, 27.0),
    ("27-30", 27.0, 30.0),
    ("30-35", 30.0, 35.0),
    ("35-45", 35.0, 45.0),
    ("45+",   45.0, 90.001),
]

_ASPECT_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _aspect_bucket(deg: float | None) -> str | None:
    """Convert a 0–360 aspect to its 8-point cardinal bucket name."""
    if deg is None:
        return None
    # Shift by half-bucket so [-22.5, 22.5) → 0 (=N), etc.
    idx = int(((deg + 22.5) % 360) // 45)
    return _ASPECT_NAMES[idx]


class SlopeSample(BaseModel):
    distance_m: float
    elevation_m: float | None
    slope_deg: float | None
    aspect_deg: float | None
    aspect: str | None  # cardinal bucket — convenient for the frontend


class ProfileSummary(BaseModel):
    distance_m: float
    avg_slope_deg: float | None
    max_slope_deg: float | None
    min_slope_deg: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    start_elevation_m: float | None
    end_elevation_m: float | None
    min_elevation_m: float | None
    max_elevation_m: float | None
    # Distribution of sample points across slope-angle buckets, e.g.
    # {"30-35": 0.18, "35-45": 0.04, …}. Values sum to ≤ 1 (sum < 1 when
    # some samples lacked slope data).
    slope_distribution: dict[str, float]
    # Distribution across the 8-point aspect cardinal buckets.
    aspect_distribution: dict[str, float]


class ProfileResponse(BaseModel):
    summary: ProfileSummary
    samples: list[SlopeSample]


def _summarise(samples: list) -> dict:
    slopes = [s.slope_deg for s in samples if s.slope_deg is not None]
    elevs = [s.elevation_m for s in samples if s.elevation_m is not None]
    aspects_bucketed = [_aspect_bucket(s.aspect_deg) for s in samples]
    aspects_bucketed = [a for a in aspects_bucketed if a is not None]

    gain = loss = 0.0
    for a, b in zip(elevs, elevs[1:]):
        delta = b - a
        if delta > 0:
            gain += delta
        else:
            loss += abs(delta)

    n = len(samples)
    slope_dist: dict[str, float] = {}
    for label, lo, hi in _SLOPE_BUCKETS:
        count = sum(1 for s in slopes if lo <= s < hi)
        slope_dist[label] = round(count / n, 3) if n else 0.0

    aspect_counts = Counter(aspects_bucketed)
    aspect_dist: dict[str, float] = {
        a: round(aspect_counts.get(a, 0) / n, 3) if n else 0.0
        for a in _ASPECT_NAMES
    }

    return {
        "avg_slope_deg": round(sum(slopes) / len(slopes), 2) if slopes else None,
        "max_slope_deg": round(max(slopes), 2) if slopes else None,
        "min_slope_deg": round(min(slopes), 2) if slopes else None,
        "elevation_gain_m": round(gain, 1) if elevs else None,
        "elevation_loss_m": round(loss, 1) if elevs else None,
        "start_elevation_m": round(elevs[0], 1) if elevs else None,
        "end_elevation_m": round(elevs[-1], 1) if elevs else None,
        "min_elevation_m": round(min(elevs), 1) if elevs else None,
        "max_elevation_m": round(max(elevs), 1) if elevs else None,
        "slope_distribution": slope_dist,
        "aspect_distribution": aspect_dist,
    }


@router.get("/profile", response_model=ProfileResponse)
async def terrain_profile(
    start_lng: float = Query(..., ge=-180, le=180),
    start_lat: float = Query(..., ge=-90, le=90),
    end_lng: float = Query(..., ge=-180, le=180),
    end_lat: float = Query(..., ge=-90, le=90),
    region: str = Query(default="colorado"),
    n: int = Query(default=64, ge=10, le=256),
    settings: Settings = Depends(get_settings),
    _auth: User = Depends(get_current_user),
) -> ProfileResponse:
    """Sample slope, elevation, and aspect along a line; return per-point
    samples plus an aggregate trip summary (slope/aspect distributions,
    elevation min/max/gain/loss, CAIC zones crossed)."""
    validate_region(region)
    _, _, dist_m = _GEOD.inv(start_lng, start_lat, end_lng, end_lat)
    if dist_m > _MAX_PROFILE_DISTANCE_M:
        raise HTTPException(
            400,
            f"Profile distance {dist_m/1000:.0f} km exceeds the "
            f"{_MAX_PROFILE_DISTANCE_M/1000:.0f} km limit",
        )
    try:
        samples = sample_profile(
            start=(start_lng, start_lat),
            end=(end_lng, end_lat),
            region=region,
            settings=settings,
            n=n,
        )
    except Exception as exc:
        logger.error("terrain profile failed: %s", exc)
        raise HTTPException(502, f"COG read failed: {exc}") from exc

    return ProfileResponse(
        summary=ProfileSummary(
            distance_m=round(samples[-1].distance_m, 1),
            **_summarise(samples),
        ),
        samples=[
            SlopeSample(
                distance_m=round(s.distance_m, 1),
                elevation_m=round(s.elevation_m, 1) if s.elevation_m is not None else None,
                slope_deg=round(s.slope_deg, 2) if s.slope_deg is not None else None,
                aspect_deg=round(s.aspect_deg, 1) if s.aspect_deg is not None else None,
                aspect=_aspect_bucket(s.aspect_deg),
            )
            for s in samples
        ],
    )
