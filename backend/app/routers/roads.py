"""Road conditions: GET /roads?region=&t= and GET /tiles/roads/segments/<rev>.geojson.

/roads is deliberately open to signed-out installs: it needs a tile token
(every install has one, see app/tile_tokens.py) instead of sign-in, plus
the per-IP `roads` rate limit. It serves only what the job stored from
CDOT's public traveler information (never a call to CDOT per request), so
the token and limit are there to stop scraping. CLAUDE.md lists this
exception. The segment lines are under /tiles: they change about once a
day, carry their revision in the path and are cached by Cloudflare under
the /tiles token rules. docs/runbook.md §11.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app import tile_tokens
from app.http_retry import CircuitOpenError
from app.rate_limit import client_ip
from app.regions import validate_region
from app.services import roads
from app.tile_tokens import require_tile_access

logger = logging.getLogger("whumpf.roads")

_NO_STORE = {"Cache-Control": "no-store"}
_UNREADABLE = (httpx.HTTPError, CircuitOpenError, ValueError)


def require_roads_access(
    request: Request,
    t: str | None = Query(None, description="Tile token from POST /tiles/token"),
) -> None:
    """A valid tile token (any tier; tokenless is never allowed here), then
    the per-IP `roads` limit."""
    if t is None or tile_tokens.verify(t) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tile token required" if t is None else "Invalid or expired tile token",
            headers=_NO_STORE,
        )
    tile_tokens.enforce("roads", client_ip(request))


router = APIRouter(prefix="/roads", tags=["roads"], dependencies=[Depends(require_roads_access)])
tiles_router = APIRouter(prefix="/tiles", tags=["tiles"], dependencies=[Depends(require_tile_access)])


def _feature(event: dict, now: datetime) -> dict:
    props = {k: v for k, v in event.items() if k != "geometry"}
    props["active"] = roads.active(event, now)
    return {"type": "Feature", "id": f"{event['feed']}:{event['id']}", "geometry": event["geometry"],
            "properties": props}


@router.get("")
async def road_conditions(region: str = Query("colorado")) -> JSONResponse:
    """Road conditions per segment, incidents and planned events.

    `conditions` maps a segment id to its `category`, CDOT's own condition
    texts and the NDFD forecast; join it to the lines at `segments.url` by
    id. `features` are incidents and planned events (not yet ended) with
    `category`, CDOT's `type` and `message`, the decoded lanes and `active`
    (in effect now; null if unknown). `category` is one of
    roads.CATEGORIES; "other" means CDOT's text is all there is. `sources`
    gives each feed's state (ok, stale, missing, expired, unavailable,
    not_configured, not_covered) and fetch time; an expired feed is left
    out. `disclaimer` must be shown with the data (CDOT's terms)."""
    validate_region(region)
    now = datetime.now(UTC)
    sources: dict[str, dict] = {}
    conditions: dict = {}
    features: list[dict] = []
    segments = None
    covered = region in roads.REGIONS and roads.configured()
    unreadable = 0
    for feed in roads.FEEDS:
        entry: dict = {"fetched": None}
        sources[feed] = entry
        if not covered:
            entry["state"] = "not_covered" if region not in roads.REGIONS else "not_configured"
            continue
        try:
            doc = await roads.read_doc(region, feed)
        except _UNREADABLE as exc:
            logger.warning("roads %s for %s unreadable: %s", feed, region, type(exc).__name__)
            entry["state"] = "unavailable"
            unreadable += 1
            continue
        entry["state"] = roads.state(doc, feed, now)
        if doc is None:
            continue
        entry["fetched"] = doc["fetched"]
        if entry["state"] == "expired":
            continue
        if feed == "conditions":
            conditions = doc["segments"]
        else:
            features.extend(
                _feature(e, now) for e in doc["events"]
                if not e.get("end") or datetime.fromisoformat(e["end"]) > now
            )
    if covered and unreadable == len(roads.FEEDS):
        raise HTTPException(503, "road conditions temporarily unavailable", headers=_NO_STORE)
    if covered:
        try:
            pointer = await roads.read_doc(region, "segments")
        except _UNREADABLE as exc:
            logger.warning("roads segments for %s unreadable: %s", region, type(exc).__name__)
            pointer = None
        if pointer:
            segments = {
                "url": f"/tiles/roads/segments/{pointer['revision']}.geojson?region={region}",
                "revision": pointer["revision"],
                "fetched": pointer["fetched"],
            }
    body = {
        "type": "FeatureCollection",
        "region": region,
        "disclaimer": roads.DISCLAIMER,
        "attribution": roads.ATTRIBUTION,
        "categories": list(roads.CATEGORIES),
        "sources": sources,
        "segments": segments,
        "conditions": conditions,
        "features": features,
    }
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=60"})


_SEGMENTS = {"Cache-Control": "public, max-age=86400"}
_SEGMENTS_GONE = {"Cache-Control": "public, max-age=60"}


@tiles_router.get("/roads/segments/{revision}.geojson")
async def road_segments(revision: str, region: str = "colorado") -> Response:
    """The road segment lines (LineString per segment, id, name,
    description, route), simplified to about 20 m. Never changes once
    written, so cached for a day. 404 once cleaned up (after 7 days)."""
    validate_region(region)
    if region not in roads.REGIONS or not roads.REVISION.match(revision):
        raise HTTPException(404, "no such segment file", headers=_SEGMENTS_GONE)
    try:
        body = await roads.read_segments(region, revision)
    except (CircuitOpenError, httpx.HTTPError) as exc:
        logger.warning("roads segments %s unreadable for %s: %s", revision, region, type(exc).__name__)
        raise HTTPException(503, "road segments temporarily unavailable", headers=_NO_STORE) from None
    if body is None:
        raise HTTPException(404, "no such segment file", headers=_SEGMENTS_GONE)
    return Response(
        content=body, media_type="application/geo+json",
        headers={**_SEGMENTS, "Content-Encoding": "gzip"},
    )
