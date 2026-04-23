"""Tile proxy — applies server-side colormaps for layers TiTiler can't handle
via the colormap_name parameter (custom or too long for browser URLs)."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Response

from app.config import get_settings

router = APIRouter(prefix="/tiles", tags=["tiles"])
logger = logging.getLogger("whumpf.tiles")

_BUCKET = "dem-cogs"


def _s3(path: str) -> str:
    return f"s3://{_BUCKET}/{path}"


def _lerp_rgba(a: list[int], b: list[int], t: float) -> list[int]:
    return [round(a[k] + (b[k] - a[k]) * t) for k in range(4)]


def _build_slope_colormap() -> dict[str, list[int]]:
    """CalTopo V1 slope-angle colormap (rescale=0,60 → pixel 0-255).

    Stops:   0° (p=0)   → transparent
            15° (p=64)  → green  #1a9641
            27° (p=115) → yellow #ffeb00
            40° (p=170) → red    #d7191c
            60° (p=255) → blue   #2b7bb9
    """
    stops: list[tuple[int, list[int]]] = [
        (0,   [0,   0,   0,   0]),
        (64,  [26,  150, 65,  255]),
        (115, [255, 235, 0,   255]),
        (170, [215, 25,  28,  255]),
        (255, [43,  123, 185, 255]),
    ]
    cmap: dict[str, list[int]] = {}
    for i in range(256):
        lo, hi = stops[0], stops[-1]
        for j in range(len(stops) - 1):
            if stops[j][0] <= i <= stops[j + 1][0]:
                lo, hi = stops[j], stops[j + 1]
                break
        span = hi[0] - lo[0]
        t = (i - lo[0]) / span if span else 0.0
        cmap[str(i)] = _lerp_rgba(lo[1], hi[1], t)
    return cmap


_SLOPE_CMAP = json.dumps(_build_slope_colormap())


@router.get("/slope/{z}/{x}/{y}")
async def slope_tile(z: int, x: int, y: int, region: str = "sanjuans") -> Response:
    """Proxy a slope tile from TiTiler with the CalTopo V1 color ramp."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png",
                params={
                    "url": _s3(f"{region}/slope.tif"),
                    "rescale": "0,60",
                    "nodata": "-9999",
                    "colormap": _SLOPE_CMAP,
                },
            )
        except httpx.RequestError as exc:
            logger.error("TiTiler unreachable: %s", exc)
            raise HTTPException(502, "tile server unreachable") from exc

    if resp.status_code == 404:
        return Response(status_code=204)
    if resp.status_code != 200:
        logger.warning("TiTiler returned %d for slope tile %d/%d/%d", resp.status_code, z, x, y)
        raise HTTPException(502, f"TiTiler returned {resp.status_code}")

    return Response(
        content=resp.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
