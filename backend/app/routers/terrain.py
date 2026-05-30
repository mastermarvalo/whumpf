"""Terrain analysis endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from pyproj import Geod

from app.auth.dependencies import get_current_user
from app.config import Settings, get_settings, validate_region
from app.models.user import User
from app.services.cog_sampler import sample_profile
from app.services.profile_summary import (
    ProfileSummary,
    SlopeSample,
    _aspect_bucket,
    summarise as _summarise,
)

router = APIRouter(prefix="/terrain", tags=["terrain"])
logger = logging.getLogger("whumpf.terrain")

# Cap a single profile request to a reasonable backcountry-route distance.
# Anything longer is almost certainly a bug or abuse — sampling a continental-scale
# line costs egress for tiny per-pixel value and provides no real-world utility.
_MAX_PROFILE_DISTANCE_M = 200_000  # 200 km

_GEOD = Geod(ellps="WGS84")


class ProfileResponse(BaseModel):
    summary: ProfileSummary
    samples: list[SlopeSample]


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
