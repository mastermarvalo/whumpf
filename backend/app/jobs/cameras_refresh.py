"""Hourly job: camera lists from NPS and Windy -> MinIO, one file per source.

    make cameras-refresh            (hourly host cron, docs/runbook.md §10)
    make cameras-refresh ARGS=--dry-run
    python -m app.jobs.cameras_refresh [--region colorado] [--source nps] [--force] [--dry-run]

Each run asks a source only when its list is older than its interval
(cameras.INTERVAL: NPS 6 h, Windy 24 h), so most runs do nothing. A source
with no API key is skipped. Each source is fetched and checked on its own;
a list that fails (an error, no cameras, or under half the cameras of the
list it would replace) is never written, and the old one stays served.
Exits 1 if any source failed.

Layout and sources: app/services/cameras.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings
from app.http_retry import CircuitOpenError, call_with_resilience
from app.regions import REGIONS
from app.services import cameras, cog_sampler
from app.services.cameras import NPS_API, WINDY_API, parse_http_date
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store, iso

logger = logging.getLogger("whumpf.cameras_refresh")

# Runs are hourly; don't skip a source because its last run started a few
# minutes later in the hour.
SLACK = timedelta(minutes=10)
MIN_KEEP_SHARE = 0.5
MAX_PAGE_BYTES = 2 * 1024 * 1024

# Windy free tier: 50 per call, offset at most 1000.
WINDY_PAGE = 50
WINDY_MAX_OFFSET = 1000

# NPS gives a park-centre point for cameras it has no position for (several
# cameras then share it). These are set by hand from each camera's page;
# a camera with a shared or missing point and no entry here is left out.
# (lon, lat), approximate.
NPS_POSITIONS: dict[str, tuple[float, float]] = {
    # Beaver Meadows Entrance/U.S. Hwy 36: the entrance station, looking east.
    "81B4686B-1DD8-B71B-0B122CB864D072FC": (-105.5605, 40.3664),
    # Kawuneeche Valley: Harbison Meadow, 0.5 mi north of the Grand Lake Entrance.
    "81B4682A-1DD8-B71B-0BBC127D2E00ACD9": (-105.8375, 40.2790),
}
# What nps.gov shows instead of an image for a camera that is off.
NPS_INACTIVE = "inactive_webcam"


def _in_bounds(region: str, lon: float, lat: float) -> bool:
    west, south, east, north = REGIONS[region].max_bounds
    return west <= lon <= east and south <= lat <= north


async def _get(client: httpx.AsyncClient, upstream: str, url: str, **kwargs) -> httpx.Response:
    async def _do() -> httpx.Response:
        r = await client.get(url, **kwargs)
        r.raise_for_status()
        return r

    return await call_with_resilience(upstream, _do)


# ── NPS ───────────────────────────────────────────────────────────────────────

class _WebcamImage(HTMLParser):
    """The src of <img id="webcamRefreshImage"> on an nps.gov webcam page."""

    def __init__(self) -> None:
        super().__init__()
        self.src: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img" and attrs.get("id") == "webcamRefreshImage" and self.src is None:
            self.src = attrs.get("src")


def nps_image_url(page_url: str, html: str) -> str | None:
    """The live image on a webcam page, or None if the camera is off."""
    parser = _WebcamImage()
    parser.feed(html)
    if not parser.src or NPS_INACTIVE in parser.src:
        return None
    url = urljoin(page_url, parser.src)
    return url if urlsplit(url).scheme == "https" else None


async def fetch_nps(client: httpx.AsyncClient, region: str) -> list[dict]:
    headers = {"X-Api-Key": get_settings().nps_api_key}
    out: list[dict] = []
    for park in cameras.NPS_PARKS.get(region, []):
        r = await _get(client, "nps", f"{NPS_API}/webcams",
                       params={"parkCode": park, "limit": 50}, headers=headers)
        listed = r.json()["data"]
        shared = Counter(
            (c.get("latitude"), c.get("longitude")) for c in listed if c.get("latitude") is not None
        )
        for cam in listed:
            nps_id, title, page = cam["id"], cam["title"], cam.get("url") or ""
            if cam.get("status") != "Active":
                continue
            point = (cam.get("latitude"), cam.get("longitude"))
            if nps_id in NPS_POSITIONS:
                lon, lat = NPS_POSITIONS[nps_id]
            elif point[0] is not None and shared[point] == 1:
                lat, lon = float(point[0]), float(point[1])
            else:
                logger.info("nps: %s has no position of its own, left out", title)
                continue
            if not _in_bounds(region, lon, lat):
                continue
            if urlsplit(page).scheme != "https" or urlsplit(page).hostname != "www.nps.gov":
                logger.warning("nps: %s has an unexpected page URL, left out", title)
                continue
            try:
                html = (await _get(client, "nps", page)).text[:MAX_PAGE_BYTES]
            except (httpx.HTTPError, CircuitOpenError) as exc:
                logger.warning("nps: page for %s failed: %s", title, type(exc).__name__)
                continue
            image = nps_image_url(page, html)
            if image is None:
                logger.info("nps: %s shows no live image, left out", title)
                continue
            updated = None
            try:
                head = await client.head(image, follow_redirects=True)
                if head.status_code == 200:
                    updated = parse_http_date(head.headers.get("last-modified"))
            except httpx.HTTPError:
                pass
            parks = cam.get("relatedParks") or [{}]
            park_name = parks[0].get("fullName") or "National Park Service"
            out.append({
                "id": f"nps:{nps_id}",
                "name": title.strip(),
                "source": "nps",
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "credit": f"NPS / {park_name}",
                "link": page,
                "image_url": image,
                "updated": updated,
            })
    return out


# ── Windy ─────────────────────────────────────────────────────────────────────

async def fetch_windy(client: httpx.AsyncClient, region: str) -> list[dict]:
    """Every active webcam in the region's bounds: position, title and page.
    No image URLs: they expire and are fetched per tap."""
    west, south, east, north = REGIONS[region].max_bounds
    headers = {"x-windy-api-key": get_settings().windy_webcams_api_key}
    out: list[dict] = []
    offset = 0
    while True:
        r = await _get(client, "windy", f"{WINDY_API}/webcams", headers=headers, params={
            "bbox": f"{north},{east},{south},{west}",
            "limit": WINDY_PAGE,
            "offset": offset,
            "include": "location,urls",
        })
        body = r.json()
        total = int(body["total"])
        for cam in body["webcams"]:
            if cam.get("status") != "active":
                continue
            loc = cam["location"]
            lon, lat = float(loc["longitude"]), float(loc["latitude"])
            if not _in_bounds(region, lon, lat):
                continue
            out.append({
                "id": f"windy:{cam['webcamId']}",
                "name": (cam.get("title") or "").strip() or "Webcam",
                "source": "windy",
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "credit": "windy.com",
                "link": cam["urls"]["detail"],
                "image_url": None,
                "updated": cam.get("lastUpdatedOn"),
            })
        offset += WINDY_PAGE
        if offset >= total:
            break
        if offset > WINDY_MAX_OFFSET:
            logger.warning("windy: %s has %d webcams; the free tier stops at %d",
                           region, total, WINDY_MAX_OFFSET + WINDY_PAGE)
            break
    return out


FETCHERS = {"nps": fetch_nps, "windy": fetch_windy}


# ── one source ────────────────────────────────────────────────────────────────

def _add_elevations(region: str, cams: list[dict], previous: list[dict]) -> None:
    """elevation_m from our DEM; kept from the last list for cameras that
    haven't moved. None where the DEM has no data or can't be read."""
    known = {(c["id"], c["lon"], c["lat"]): c.get("elevation_m") for c in previous}
    todo = []
    for cam in cams:
        cam["elevation_m"] = known.get((cam["id"], cam["lon"], cam["lat"]))
        if cam["elevation_m"] is None:
            todo.append(cam)
    if not todo:
        return
    try:
        values = cog_sampler.sample_elevations(
            [(c["lon"], c["lat"]) for c in todo], region, get_settings(),
        )
    except Exception:
        logger.warning("%s: DEM elevations failed", region, exc_info=True)
        return
    for cam, metres in zip(todo, values, strict=True):
        cam["elevation_m"] = None if metres is None else round(metres)


def _load(store: Store, region: str, source: str) -> dict | None:
    raw = store.get(cameras.key(region, source))
    return json.loads(raw) if raw else None


async def refresh_source(
    client: httpx.AsyncClient, store: Store, region: str, source: str,
    now: datetime, force: bool = False,
) -> bool:
    """Returns True if the source failed."""
    if not cameras.configured(source):
        logger.info("%s %s: no API key, skipped", region, source)
        return False
    old = _load(store, region, source)
    if old and not force:
        age = now - datetime.fromisoformat(old["fetched"])
        if age < cameras.INTERVAL[source] - SLACK:
            return False
    previous = (old or {}).get("cameras", [])
    try:
        cams = await FETCHERS[source](client, region)
        if not cams:
            raise BadFile("no cameras")
        if len(cams) < MIN_KEEP_SHARE * len(previous):
            raise BadFile(f"{len(cams)} cameras, the served list has {len(previous)}")
    except Exception as exc:  # noqa: BLE001 - one source must never stop the others
        # The type and message only: httpx messages carry the URL, and the
        # keys are never in a URL.
        logger.error("%s %s: refresh failed, keeping the served list: %s: %s",
                     region, source, type(exc).__name__, exc)
        return True
    await asyncio.to_thread(_add_elevations, region, cams, previous)
    cams.sort(key=lambda c: c["id"])
    doc = {"region": region, "source": source, "fetched": iso(now), "count": len(cams), "cameras": cams}
    store.put(cameras.key(region, source), json.dumps(doc, indent=1).encode(), "application/json")
    logger.info("%s %s: %d cameras", region, source, len(cams))
    return False


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    sources: list[str] | None = None, now: datetime | None = None, force: bool = False,
) -> bool:
    """Refresh every region and source that is due. True if all went well."""
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        for source in sources or cameras.SOURCES:
            try:
                failed = await refresh_source(client, store, region, source, now, force)
            except Exception:
                # MinIO: reading or writing the list. The next source still runs.
                logger.exception("%s %s: refresh failed", region, source)
                failed = True
            ok = ok and not failed
    return ok


async def _main(regions: list[str], sources: list[str] | None, dry_run: bool, force: bool) -> bool:
    headers = {"User-Agent": get_settings().weather_user_agent}
    store: Store = DryRunStore(S3Store()) if dry_run else S3Store()
    async with httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True) as client:
        return await run(store, client, regions, sources, force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", choices=sorted(REGIONS), action="append",
                        help="default: every region")
    parser.add_argument("--source", choices=cameras.SOURCES, action="append",
                        help="default: every source")
    parser.add_argument("--force", action="store_true",
                        help="ask every source now, even if its list is recent")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and check, but write nothing to MinIO")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ok = asyncio.run(_main(args.region or sorted(REGIONS), args.source, args.dry_run, args.force))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
