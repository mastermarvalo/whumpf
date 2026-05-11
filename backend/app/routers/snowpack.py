"""Snowpack data endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import get_current_user
from app.models.user import User
from app.services.snotel import fetch_station_history, fetch_stations_geojson

router = APIRouter(prefix="/snowpack", tags=["snowpack"])
logger = logging.getLogger("whumpf.snowpack")


@router.get("/stations/history")
async def snotel_station_history(
    triplet: str,
    days: int = 30,
    _auth: User = Depends(get_current_user),
) -> list:
    """Daily SWE + snow depth timeseries for a single SNOTEL station (up to 90 days)."""
    try:
        return await fetch_station_history(triplet, days=min(days, 90))
    except Exception as exc:
        logger.error("SNOTEL history fetch failed: %s", exc)
        raise HTTPException(502, f"SNOTEL history unavailable: {exc}") from exc


@router.get("/stations")
async def snotel_stations(_auth: User = Depends(get_current_user)) -> dict:
    """GeoJSON FeatureCollection of active Colorado SNOTEL stations with current readings."""
    try:
        return await fetch_stations_geojson()
    except Exception as exc:
        logger.error("SNOTEL fetch failed: %s", exc)
        raise HTTPException(502, f"SNOTEL unavailable: {exc}") from exc
