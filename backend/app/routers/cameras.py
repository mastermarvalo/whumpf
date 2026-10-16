"""Mountain cameras: GET /cameras?region=&t= and GET /cameras/{id}/image.

Deliberately open to signed-out installs: it needs a tile token (every
install has one, see app/tile_tokens.py) instead of sign-in, plus the
per-IP `cameras` rate limit. Requiring the token keeps /image from being a
free anonymous way to spend our Windy key; those lookups also count against
a global `windy_upstream` limit. The cameras are public NPS and Windy
webcams. Windy's free tier requires the layer to be free to every user, so
it must never sit behind a paid tier. CLAUDE.md lists this exception.
docs/runbook.md §10.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app import tile_tokens
from app.http_retry import CircuitOpenError
from app.rate_limit import client_ip
from app.regions import validate_region
from app.services import cameras

logger = logging.getLogger("whumpf.cameras")

_NO_STORE = {"Cache-Control": "no-store"}
# What reading a stored list from MinIO can raise (rasterlayer.read_pointer).
_UNREADABLE = (httpx.HTTPError, CircuitOpenError, ValueError)


def require_cameras_access(
    request: Request,
    t: str | None = Query(None, description="Tile token from POST /tiles/token"),
) -> None:
    """A valid tile token (any tier; tokenless is never allowed here), then
    the per-IP `cameras` limit."""
    if t is None or tile_tokens.verify(t) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tile token required" if t is None else "Invalid or expired tile token",
            headers=_NO_STORE,
        )
    tile_tokens.enforce("cameras", client_ip(request))


router = APIRouter(
    prefix="/cameras", tags=["cameras"], dependencies=[Depends(require_cameras_access)]
)


def _state(doc: dict | None, source: str, now: datetime) -> str:
    if doc is None:
        return "missing"
    age = now - datetime.fromisoformat(doc["fetched"])
    if age > cameras.MAX_AGE:
        return "expired"
    if age > cameras.STALE_AFTER[source]:
        return "stale"
    return "ok"


def _feature(camera: dict, region: str) -> dict:
    return {
        "type": "Feature",
        "id": camera["id"],
        "geometry": {"type": "Point", "coordinates": [camera["lon"], camera["lat"]]},
        "properties": {
            "id": camera["id"],
            "name": camera["name"],
            "source": camera["source"],
            "elevation_m": camera.get("elevation_m"),
            "credit": camera["credit"],
            "link": camera["link"],
            "updated": camera.get("updated"),
            "image_url": camera.get("image_url"),
            "image": f"/cameras/{camera['id']}/image?region={region}",
        },
    }


@router.get("")
async def list_cameras(region: str = Query("colorado")) -> JSONResponse:
    """Every camera in the region as GeoJSON points. `image_url` is set when
    the app may load the image straight from the source (NPS); otherwise
    (Windy) GET `image` on tap. `sources` gives each source's state (ok,
    stale, missing, expired, unavailable, not_configured), its last fetch
    and the credit to show while the layer is on. One source down leaves
    the others' cameras in place."""
    validate_region(region)
    now = datetime.now(UTC)
    features: list[dict] = []
    sources: dict[str, dict] = {}
    configured = unreadable = 0
    for source in cameras.SOURCES:
        entry: dict = {"attribution": cameras.ATTRIBUTION[source], "fetched": None}
        sources[source] = entry
        if not cameras.configured(source):
            entry["state"] = "not_configured"
            continue
        configured += 1
        try:
            doc = await cameras.read_list(region, source)
        except _UNREADABLE as exc:
            logger.warning("cameras %s list for %s unreadable: %s", source, region, type(exc).__name__)
            entry["state"] = "unavailable"
            unreadable += 1
            continue
        entry["state"] = _state(doc, source, now)
        if doc is None:
            continue
        entry["fetched"] = doc["fetched"]
        if entry["state"] != "expired":
            features.extend(_feature(c, region) for c in doc["cameras"])
    if configured and unreadable == configured:
        raise HTTPException(503, "camera list temporarily unavailable", headers=_NO_STORE)
    body = {"type": "FeatureCollection", "region": region, "sources": sources, "features": features}
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=300"})


@router.get("/{camera_id}/image")
async def camera_image(camera_id: str, region: str = Query("colorado")) -> JSONResponse:
    """The camera's current image: `url` to load, `taken` (when the source
    last updated it, if known) and `expires` (Windy URLs stop working
    then; null for NPS). 404 for a camera that isn't in the list, 502 if
    the source can't give an image now."""
    validate_region(region)
    if not cameras.ID.match(camera_id):
        raise HTTPException(404, "no such camera", headers=_NO_STORE)
    source = camera_id.split(":", 1)[0]
    if not cameras.configured(source):
        raise HTTPException(404, "no such camera", headers=_NO_STORE)
    try:
        doc = await cameras.read_list(region, source)
    except _UNREADABLE as exc:
        logger.warning("cameras %s list for %s unreadable: %s", source, region, type(exc).__name__)
        raise HTTPException(503, "camera list temporarily unavailable", headers=_NO_STORE) from None
    camera = cameras.find(doc, camera_id)
    if camera is None:
        raise HTTPException(404, "no such camera", headers=_NO_STORE)
    try:
        image = await cameras.image_for(camera)
    except cameras.Gone:
        raise HTTPException(404, "no such camera", headers=_NO_STORE) from None
    except cameras.Unavailable:
        raise HTTPException(502, "camera image unavailable", headers=_NO_STORE) from None
    body = {"id": camera_id, **image, "credit": camera["credit"], "link": camera["link"]}
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=60"})
