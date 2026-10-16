"""Mountain cameras: shared pieces.

The hourly job (app/jobs/cameras_refresh.py) keeps one camera list per
source and region in MinIO, each written only when that source answered
with a list that passed the checks:

    dem-cogs/cameras/<region>/<source>.json

GET /cameras (routers/cameras.py) merges them; one source down never
empties the layer. Sources and their terms (docs/runbook.md §10):

- nps: NPS API webcams (developer.nps.gov) for the parks in NPS_PARKS.
  Public domain; the live JPEGs are on nps.gov and the app loads them
  straight from there.
- windy: Windy Webcams API, free tier. Images can't be cached or rehosted
  and their URLs expire after 10 minutes, so the list has no image URL;
  the app asks GET /cameras/{id}/image for a fresh one on each tap. Every
  image links to its windy.com page, and the credit line in ATTRIBUTION
  must show with the layer. Free tier: the layer must never sit behind a
  paid tier (CLAUDE.md).

Camera ids are "<source>:<source id>".
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app import tile_tokens
from app.config import get_settings
from app.http_retry import CircuitOpenError, call_with_resilience
from app.services import rasterlayer

logger = logging.getLogger("whumpf.cameras")

SOURCES = ("nps", "windy")

WINDY_API = "https://api.windy.com/webcams/api/v3"
NPS_API = "https://developer.nps.gov/api/v1"

# How often the job asks each source, and when /status calls it stale.
INTERVAL = {"nps": timedelta(hours=6), "windy": timedelta(hours=24)}
STALE_AFTER = {source: 3 * every for source, every in INTERVAL.items()}
# Cameras don't move, so an old list is still worth showing; past this the
# API leaves the source out.
MAX_AGE = timedelta(days=7)

ATTRIBUTION = {
    "nps": {
        "text": "NPS / Rocky Mountain National Park",
        "links": [{"text": "nps.gov", "url": "https://www.nps.gov/romo/learn/photosmultimedia/webcams.htm"}],
    },
    # Windy's required courtesy line, with both links.
    "windy": {
        "text": "Webcams provided by windy.com — add a webcam",
        "links": [
            {"text": "windy.com", "url": "https://www.windy.com/"},
            {"text": "add a webcam", "url": "https://www.windy.com/webcams/add"},
        ],
    },
}

# NPS parks to list per region.
NPS_PARKS = {"colorado": ["romo"]}

ID = re.compile(r"^(nps|windy):[A-Za-z0-9-]{1,64}$")


def configured(source: str) -> bool:
    """A source without an API key is skipped by the job and the API."""
    settings = get_settings()
    return bool(settings.nps_api_key if source == "nps" else settings.windy_webcams_api_key)


def key(region: str, source: str) -> str:
    return f"cameras/{region}/{source}.json"


_lists: dict[str, tuple[dict | None, float]] = {}


async def read_list(region: str, source: str) -> dict | None:
    """The stored list for one source, or None if the job hasn't written it.
    Raises if MinIO can't be read."""
    return await rasterlayer.read_pointer(key(region, source), _lists)


def age_problem(source: str, doc: dict | None, now: datetime) -> str | None:
    """A /status problem string, or None if the source is fine."""
    if not configured(source):
        return None
    return rasterlayer.age_problem(source, (doc or {}).get("fetched"), STALE_AFTER[source], now)


def find(doc: dict | None, camera_id: str) -> dict | None:
    for camera in (doc or {}).get("cameras", []):
        if camera["id"] == camera_id:
            return camera
    return None


# ── the image for one camera, on tap ──────────────────────────────────────────

class Unavailable(Exception):
    """The source couldn't give an image right now."""


class Gone(Exception):
    """The source no longer has this camera."""


# Windy image URLs expire 10 minutes after the API hands them out; keep them
# for half that. NPS images are re-checked for their age once a minute.
WINDY_URL_LIFETIME = timedelta(minutes=10)
WINDY_URL_TTL_S = 300.0
NPS_HEAD_TTL_S = 60.0
_CACHE_MAX = 2000
# Windy free tier sizes, best first. "preview" is about 400x224.
WINDY_SIZES = ("preview", "thumbnail", "icon")

_HTTP = httpx.AsyncClient(timeout=8.0)
_images: dict[str, tuple[dict, float]] = {}


def _remember(camera_id: str, answer: dict) -> dict:
    if len(_images) >= _CACHE_MAX:
        _images.clear()
    _images[camera_id] = (answer, time.monotonic())
    return answer


async def image_for(camera: dict, now: datetime | None = None) -> dict:
    """{url, taken, expires} for a camera from a stored list. Raises Gone or
    Unavailable (Windy only; an NPS image is always its stored URL)."""
    now = now or datetime.now(UTC)
    source = camera["source"]
    cached = _images.get(camera["id"])
    ttl = WINDY_URL_TTL_S if source == "windy" else NPS_HEAD_TTL_S
    if cached and time.monotonic() - cached[1] < ttl:
        return cached[0]
    if source == "windy":
        return _remember(camera["id"], await _windy_image(camera, now))
    return _remember(camera["id"], await _nps_image(camera))


async def _nps_image(camera: dict) -> dict:
    """The stored URL, with its Last-Modified as the age when nps.gov gives
    one (else the age the job saw)."""
    taken = camera.get("updated")
    try:
        r = await call_with_resilience(
            "nps_image", lambda: _HTTP.head(camera["image_url"], follow_redirects=True),
        )
        if r.status_code == 200:
            taken = parse_http_date(r.headers.get("last-modified")) or taken
    except (httpx.HTTPError, CircuitOpenError) as exc:
        logger.warning("nps image check for %s failed: %s", camera["id"], type(exc).__name__)
    return {"url": camera["image_url"], "taken": taken, "expires": None}


async def _windy_image(camera: dict, now: datetime) -> dict:
    """A fresh image URL from Windy. Every lookup that reaches Windy counts
    against the global `windy_upstream` limit (429 when over)."""
    tile_tokens.enforce("windy_upstream", "global")
    webcam_id = camera["id"].split(":", 1)[1]
    headers = {
        "x-windy-api-key": get_settings().windy_webcams_api_key,
        "User-Agent": get_settings().weather_user_agent,
    }

    async def _do() -> httpx.Response:
        r = await _HTTP.get(
            f"{WINDY_API}/webcams/{webcam_id}", params={"include": "images,urls"}, headers=headers,
        )
        if r.status_code != 404:
            r.raise_for_status()
        return r

    try:
        r = await call_with_resilience("windy", _do)
    except (httpx.HTTPError, CircuitOpenError) as exc:
        # The type only: the key is in this request's headers.
        logger.warning("windy image for %s failed: %s", camera["id"], type(exc).__name__)
        raise Unavailable from None
    if r.status_code == 404:
        raise Gone
    try:
        body = r.json()
        current = body["images"]["current"]
        url = next(current[size] for size in WINDY_SIZES if current.get(size))
    except (ValueError, KeyError, TypeError, StopIteration):
        logger.warning("windy image for %s: no image in the answer", camera["id"])
        raise Unavailable from None
    return {
        "url": url,
        "taken": body.get("lastUpdatedOn") or camera.get("updated"),
        "expires": rasterlayer.iso(now + WINDY_URL_LIFETIME),
    }


def parse_http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return rasterlayer.iso(parsedate_to_datetime(value).astimezone(UTC))
    except (TypeError, ValueError):
        return None
