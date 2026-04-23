"""Snowpack data endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.services.snotel import fetch_stations_geojson

router = APIRouter(prefix="/snowpack", tags=["snowpack"])
logger = logging.getLogger("whumpf.snowpack")


@router.get("/stations")
async def snotel_stations() -> dict:
    """GeoJSON FeatureCollection of active Colorado SNOTEL stations with current readings."""
    try:
        return await fetch_stations_geojson()
    except Exception as exc:
        logger.error("SNOTEL fetch failed: %s", exc)
        raise HTTPException(502, f"SNOTEL unavailable: {exc}") from exc
