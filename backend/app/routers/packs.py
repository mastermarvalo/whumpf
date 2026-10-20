"""Offline packs: GET /packs/manifest, POST /packs, GET /packs/{id}.

Signed-in users only. The contract the app builds against is
docs/api-packs.md; how packs are built and stored is docs/runbook.md §12.

- A pack is shared: the same request (region, box rounded to the zoom 14
  grid, layers, each layer's source version) gives the same id for every
  user, and a ready pack whose files are still in R2 is answered at once.
- GET /packs/{id} answers only users who asked for that pack
  (pack_requests); anyone else gets the same 404 as an unknown id.
- Limits: 10 POSTs an hour per user (the `packs` bucket), 60 polls a minute
  (`packs_poll`), at most 2 of a user's packs queued or building (429), at
  most 20 on the server (503 + Retry-After). CLAUDE.md lists the reasoning.
- Download links are presigned R2 GETs, made fresh on every call and never
  logged (they carry a signature).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tile_tokens
from app.auth.dependencies import get_current_user
from app.config import get_settings
from app.db import get_session
from app.models import Pack, PackRequest, User
from app.regions import validate_region
from app.services import packs
from app.services.pack_cache import LAYERS, PackTileError, SourceVersions

logger = logging.getLogger("whumpf.packs")

router = APIRouter(prefix="/packs", tags=["packs"])

_NO_STORE = {"Cache-Control": "no-store"}
_UNAVAILABLE = "Offline packs are not available right now."
_AVERAGES_TTL_S = 600.0

# Source versions for pack ids and the manifest (MinIO ETags cached 10 min
# per worker; the ski file's mtime from the read-only /data/martin-tiles).
_versions = SourceVersions()
_averages: dict[str, tuple[float, dict[str, dict[str, int]]]] = {}


@lru_cache
def _store() -> packs.R2Store:
    """The R2 client (raises PackBuildError if R2 isn't configured)."""
    return packs.R2Store()


class PackIn(BaseModel):
    region: str
    bbox: list[float]
    layers: list[str]


def _refuse(status_code: int, detail: str, retry_after: int | None = None) -> HTTPException:
    headers = dict(_NO_STORE)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def _unavailable() -> HTTPException:
    return _refuse(503, _UNAVAILABLE, packs.UNAVAILABLE_RETRY_AFTER_S)


def _check_region(region: str, user: User) -> None:
    validate_region(region)
    if region not in user.region_ids:
        raise _refuse(status.HTTP_403_FORBIDDEN, "Region not available for this account")


def _r2_configured() -> bool:
    try:
        packs.check_r2_settings()
    except packs.PackBuildError:
        return False
    return True


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


async def _current_versions(region: str, layers: list[str]) -> dict[str, str | None]:
    """Each layer's source version now. 503 if one can't be read, or a
    layer can't be built at all (no USGS contact, no R2)."""
    if not _r2_configured():
        raise _unavailable()
    found: dict[str, str | None] = {}
    for name in layers:
        if LAYERS[name].kind == "usgs" and not get_settings().usgs_contact:
            raise _unavailable()
        try:
            found[name] = await _versions.layer_version(name, region)
        except PackTileError as exc:
            logger.warning("packs: no source version for %s/%s: %s", region, name, exc)
            raise _unavailable() from exc
    return found


async def _present(pack: Pack) -> bool:
    """A ready pack's files are all still in R2. 503 if R2 can't say."""
    try:
        return await asyncio.to_thread(packs.files_present, _store(), pack)
    except packs.PackBuildError as exc:
        raise _unavailable() from exc
    except (ClientError, BotoCoreError) as exc:
        logger.warning("packs: can't check pack %s in R2: %s", pack.id, packs.reason(exc))
        raise _unavailable() from exc


def _take(session: Session, user: User, pid: uuid.UUID, present: bool | None, new=None) -> tuple[Pack, bool]:
    try:
        return packs.take_request(session, user.id, pid, present=present, new=new)
    except packs.PackRefused as exc:
        raise _refuse(exc.status, exc.detail, exc.retry_after) from exc


def _view(session: Session, pack: Pack, now: datetime) -> dict:
    ready = pack.status == "ready"
    files: list[dict] = []
    if ready:
        store = _store()
        files = [{"layer": f["layer"], "version": f["version"], "bytes": f["bytes"], "md5": f["md5"],
                  "url": store.presign(f["key"]) if f.get("key") else None} for f in pack.files]
    total = pack.tiles_total
    progress = 1.0 if ready else (round(min(pack.tiles_done / total, 1.0), 3) if total else 0.0)
    return {
        "id": str(pack.id),
        "status": pack.status,
        "region": pack.region,
        "bounds": [round(v, 5) for v in packs.grid_range(packs.pack_bbox(pack)).bounds()],
        "layers": list(pack.layers),
        "progress": progress,
        "tiles_done": total if ready else pack.tiles_done,
        "tiles_total": total,
        "queue_position": packs.queue_position(session, pack),
        "error": packs.FAILED_MESSAGE if pack.status == "failed" else None,
        "built_at": _iso(pack.built_at),
        "urls_expire_at": _iso(now + timedelta(seconds=packs.URL_TTL_S)) if ready else None,
        "files": files,
    }


def _answer(session: Session, pack: Pack, code: int) -> JSONResponse:
    try:
        body = _view(session, pack, datetime.now(UTC))
    except packs.PackBuildError as exc:
        raise _unavailable() from exc
    return JSONResponse(body, status_code=code, headers=_NO_STORE)


@router.get("/manifest")
async def manifest(
    region: str = Query(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """What the app needs to show a pack's layers and estimate its size."""
    tile_tokens.enforce("packs_poll", f"u:{user.id}")
    _check_region(region, user)
    cached = _averages.get(region)
    if cached is None or time.monotonic() - cached[0] > _AVERAGES_TTL_S:
        cached = (time.monotonic(), packs.tile_byte_averages(session, region))
        _averages[region] = cached
    averages = cached[1]
    session.commit()  # don't sit in a transaction during the version lookups

    r2 = _r2_configured()
    usgs = bool(get_settings().usgs_contact)
    layers = []
    for name, spec in LAYERS.items():
        version: str | None = None
        available = r2 and (usgs if spec.kind == "usgs" else True)
        if spec.kind != "usgs":
            try:
                version = await _versions.layer_version(name, region)
            except PackTileError as exc:
                logger.warning("packs: no source version for %s/%s: %s", region, name, exc)
                available = False
        entry = {
            "id": name, "minzoom": spec.minzoom, "maxzoom": spec.maxzoom,
            "tile_type": spec.tile_type, "tile_size": packs.TILE_SIZE[name],
            "pad_tiles": packs.ELEVATION_PAD if name == "elevation" else 0,
        }
        if name == "elevation":
            entry["encoding"] = "terrarium"
        entry.update({"attribution": packs.ATTRIBUTION[name], "source_version": version,
                      "available": available, "bytes_per_tile": averages.get(name, {})})
        layers.append(entry)

    poll = tile_tokens.BUCKETS["packs_poll"][0]
    return JSONResponse({
        "region": region,
        "max_area_km2": packs.MAX_AREA_KM2,
        "max_side_km": packs.MAX_SIDE_M // 1000,
        "grid_zoom": packs.GRID_ZOOM,
        "required_layers": list(packs.REQUIRED_LAYERS),
        "optional_layers": list(packs.OPTIONAL_LAYERS),
        "url_ttl_s": packs.URL_TTL_S,
        "limits": {
            "requests_per_hour": tile_tokens.BUCKETS["packs"][0].amount,
            "active_per_user": packs.MAX_ACTIVE_PER_USER,
            "polls_per_minute": poll.amount,
        },
        "layers": layers,
    }, headers=_NO_STORE)


@router.post("")
async def request_pack(
    body: PackIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Asks for a pack: answered ready at once, joined while it builds, or
    queued (202)."""
    tile_tokens.enforce("packs", f"u:{user.id}")
    _check_region(body.region, user)
    try:
        layers = packs.check_api_layers(body.layers)
        box_, layers = packs.check_request(body.region, body.bbox, layers)
    except ValueError as exc:
        raise _refuse(422, str(exc)) from exc

    versions = await _current_versions(body.region, layers)
    base = packs.grid_range(box_)
    pid = packs.pack_id(body.region, base, layers, versions)

    existing = session.get(Pack, pid)
    present = None
    if existing is not None and packs.is_fresh(existing, datetime.now(UTC)):
        present = await _present(existing)
    session.commit()  # nothing written; ends the read before the locked part

    pack, queued = _take(session, user, pid, present,
                         new=lambda: packs.new_api_pack(pid, body.region, base, layers, user.id))
    if queued:
        logger.info("pack %s queued for user %s", pack.id, user.id)
    return _answer(session, pack, status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK)


@router.get("/{pack_id}")
async def get_pack(
    pack_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """A pack's status and, once ready, fresh download links. A ready pack
    whose files have expired is queued again."""
    tile_tokens.enforce("packs_poll", f"u:{user.id}")
    not_found = _refuse(status.HTTP_404_NOT_FOUND, "Pack not found")
    try:
        pid = uuid.UUID(pack_id)
    except ValueError:
        raise not_found from None
    asked = session.scalar(select(PackRequest.user_id).where(
        PackRequest.pack_id == pid, PackRequest.user_id == user.id))
    pack = session.get(Pack, pid) if asked is not None else None
    if pack is None:
        raise not_found

    if pack.status == "ready":
        fresh = packs.is_fresh(pack, datetime.now(UTC))
        present = await _present(pack) if fresh else False
        if not present:
            session.commit()  # nothing written; ends the read before the locked part
            try:
                pack, _ = _take(session, user, pid, present=False)
            except LookupError:
                raise not_found from None
            logger.info("pack %s: files expired, queued again", pack.id)
    return _answer(session, pack, status.HTTP_200_OK)
