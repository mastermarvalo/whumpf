"""Terrain analysis endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.services.cog_sampler import sample_profile

router = APIRouter(prefix="/terrain", tags=["terrain"])
logger = logging.getLogger("whumpf.terrain")


class SlopeSample(BaseModel):
    distance_m: float
    elevation_m: float | None
    slope_deg: float | None


class ProfileSummary(BaseModel):
    distance_m: float
    avg_slope_deg: float | None
    max_slope_deg: float | None
    min_slope_deg: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    start_elevation_m: float | None
    end_elevation_m: float | None


class ProfileResponse(BaseModel):
    summary: ProfileSummary
    samples: list[SlopeSample]


def _summarise(samples: list) -> dict:
    slopes = [s.slope_deg for s in samples if s.slope_deg is not None]
    elevs = [s.elevation_m for s in samples if s.elevation_m is not None]

    gain = loss = 0.0
    for a, b in zip(elevs, elevs[1:]):
        delta = b - a
        if delta > 0:
            gain += delta
        else:
            loss += abs(delta)

    return {
        "avg_slope_deg": round(sum(slopes) / len(slopes), 2) if slopes else None,
        "max_slope_deg": round(max(slopes), 2) if slopes else None,
        "min_slope_deg": round(min(slopes), 2) if slopes else None,
        "elevation_gain_m": round(gain, 1) if elevs else None,
        "elevation_loss_m": round(loss, 1) if elevs else None,
        "start_elevation_m": round(elevs[0], 1) if elevs else None,
        "end_elevation_m": round(elevs[-1], 1) if elevs else None,
    }


@router.get("/profile", response_model=ProfileResponse)
def terrain_profile(
    start_lng: float,
    start_lat: float,
    end_lng: float,
    end_lat: float,
    region: str = "sanjuans",
    n: int = Query(default=64, ge=10, le=256),
    settings: Settings = Depends(get_settings),
) -> ProfileResponse:
    """Sample slope and elevation along a line between two coordinates."""
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
            )
            for s in samples
        ],
    )
