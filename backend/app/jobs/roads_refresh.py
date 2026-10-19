"""5-minute job: CDOT COtrip road conditions, incidents and planned events -> MinIO.

    make roads-refresh            (host cron every 5 min, docs/runbook.md §11)
    make roads-refresh ARGS=--dry-run
    python -m app.jobs.roads_refresh [--feed incidents] [--force] [--dry-run]

Conditions and incidents are asked every run, planned events every 30 min
and the segment lines once a day (or as soon as conditions name a segment
the lines don't have). Each feed is fetched and checked on its own; a feed
that fails (an error on any page, a body that isn't GeoJSON, or conditions
with under half the segments of the file they would replace) is never
written, and the old file stays served with its age. An empty
COTRIP_API_KEY skips everything. Exits 1 if any feed failed.

The key has to go in the URL (?apiKey=), so nothing here logs a URL, an
httpx message or a traceback: errors are logged as "HTTP 500" or the
exception type. app/log_redact.py is the backstop on every handler.

Layout and categories: app/services/roads.py.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from shapely.geometry import mapping, shape

from app import log_redact
from app.config import get_settings
from app.http_retry import CircuitOpenError, call_with_resilience
from app.services import roads
from app.services.rasterlayer import BadFile, DryRunStore, S3Store, Store, cleanup, iso

logger = logging.getLogger("whumpf.roads_refresh")

# Runs are every 5 min; don't skip a feed because this run started early.
SLACK = timedelta(minutes=2)
MIN_KEEP_SHARE = 0.5
MAX_PAGES = 50
# COtrip caps a response at 10 MB.
MAX_PAGE_BYTES = 12 * 1024 * 1024
# About 20 m; the raw lines have ~185,000 points.
SIMPLIFY_DEG = 0.0002
KEEP_SEGMENT_REVISIONS = timedelta(days=7)
EVENT_GEOMETRIES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


class FeedError(Exception):
    """A COtrip request failed. The message never contains the URL."""


def describe(exc: BaseException) -> str:
    """A log-safe description: httpx messages carry the URL, and the URL
    carries the key."""
    if isinstance(exc, (FeedError, BadFile)):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


# ── COtrip ────────────────────────────────────────────────────────────────────

def next_offset(header: str | None) -> str | None:
    """The cursor for the next page. COtrip sends "None" on the last page
    and, for roadConditions, wraps the cursor in a Python bytes repr
    (b'...'), which must be unwrapped to work."""
    if not header:
        return None
    value = header.strip()
    if m := re.fullmatch(r"b'(.*)'", value):
        value = m.group(1)
    return None if value in ("", "None", "null") else value


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    async def _do() -> httpx.Response:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r

    try:
        return await call_with_resilience("cotrip", _do)
    except httpx.HTTPStatusError as exc:
        raise FeedError(f"HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise FeedError(type(exc).__name__) from None
    except CircuitOpenError:
        raise FeedError("circuit open") from None


async def fetch_feed(client: httpx.AsyncClient, feed: str, **params) -> list[dict]:
    """Every feature of a COtrip feed, page by page. Raises FeedError if any
    page fails, so a feed is never half read."""
    query = {"apiKey": get_settings().cotrip_api_key, **params}
    features: list[dict] = []
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        r = await _get(client, f"{roads.API}/{feed}", query)
        if len(r.content) > MAX_PAGE_BYTES:
            raise FeedError(f"{feed}: a page of {len(r.content)} bytes")
        try:
            page = r.json()["features"]
            if not isinstance(page, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise FeedError(f"{feed}: not a GeoJSON FeatureCollection") from None
        features.extend(page)
        cursor = next_offset(r.headers.get("Next-Offset"))
        if cursor is None:
            return features
        if cursor in seen:
            raise FeedError(f"{feed}: paging repeats")
        seen.add(cursor)
        query["offset"] = cursor
    raise FeedError(f"{feed}: more than {MAX_PAGES} pages")


# ── parsing ───────────────────────────────────────────────────────────────────

def parse_conditions(features: list[dict]) -> tuple[dict[str, dict], Counter]:
    """Segment id -> its conditions, and the descriptions with no mapping."""
    segments: dict[str, dict] = {}
    unmapped: Counter = Counter()
    for feature in features:
        p = feature.get("properties") or {}
        if p.get("id") is None:
            continue
        entries, forecast = [], None
        for c in p.get("currentConditions") or []:
            text = (c.get("conditionDescription") or "").strip()
            if text.lower() == roads.FORECAST:
                forecast = {
                    "text": c.get("additionalData"),
                    "start": roads.ms_time(c.get("startTime")),
                    "end": roads.ms_time(c.get("endTime")),
                }
                continue
            category = roads.condition_category(text)
            if category == "other":
                unmapped[text] += 1
            entries.append({
                "category": category,
                "text": text,
                "source": c.get("sourceType"),
                "note": c.get("additionalData"),
                "start": roads.ms_time(c.get("startTime")),
                "end": roads.ms_time(c.get("endTime")),
                "updated": roads.ms_time(c.get("updateTime")),
            })
        segment_id = str(p["id"])
        segments[segment_id] = {
            "id": segment_id,
            "name": p.get("nameId") or p.get("name"),
            "description": p.get("name"),
            "route": p.get("routeName"),
            "category": roads.segment_category([e["category"] for e in entries]),
            "conditions": entries,
            "forecast": forecast,
        }
    return segments, unmapped


def _round(coords):
    if coords and isinstance(coords[0], (int, float)):
        return [round(float(v), 6) for v in coords[:2]]
    return [_round(c) for c in coords]


def parse_events(features: list[dict], feed: str, now: datetime) -> tuple[list[dict], Counter]:
    """Incidents or planned events that haven't ended, and the types that
    came out as "other"."""
    events: list[dict] = []
    unmapped: Counter = Counter()
    for feature in features:
        p = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if p.get("id") is None or geometry.get("type") not in EVENT_GEOMETRIES or not geometry.get("coordinates"):
            continue
        end = roads.iso_time(p.get("clearTime"))
        if end and datetime.fromisoformat(end) <= now:
            continue
        lanes = [roads.decode_lanes(i) for i in p.get("laneImpacts") or []]
        category = roads.event_category(p, lanes)
        if category == "other":
            unmapped[(p.get("type") or "").strip()] += 1
        events.append({
            "id": str(p["id"]),
            "feed": feed,
            "category": category,
            "type": p.get("type"),
            "cdot_category": p.get("category"),
            "name": p.get("name"),
            "route": p.get("routeName"),
            "direction": p.get("direction"),
            "severity": p.get("severity"),
            "message": p.get("travelerInformationMessage"),
            "impacts": list(p.get("additionalImpacts") or []),
            "lanes": lanes,
            "closed_directions": [lane["direction"] for lane in lanes if lane["all_closed"]],
            "start": roads.iso_time(p.get("startTime")),
            "end": end,
            "updated": roads.iso_time(p.get("lastUpdated")),
            "schedule": [
                {
                    "start": roads.iso_time(w.get("startTime")),
                    "end": roads.iso_time(w.get("endTime")),
                    "days": w.get("daysOfWeek") or None,
                }
                for w in p.get("schedule") or []
            ],
            "geometry": {"type": geometry["type"], "coordinates": _round(geometry["coordinates"])},
        })
    events.sort(key=lambda e: e["id"])
    return events, unmapped


def build_segments(features: list[dict]) -> tuple[bytes, list[str]]:
    """The segment lines, simplified to about 20 m, as gzipped GeoJSON."""
    out, ids = [], []
    for feature in features:
        p = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if p.get("id") is None or geometry.get("type") not in ("LineString", "MultiLineString"):
            continue
        try:
            line = shape(geometry).simplify(SIMPLIFY_DEG, preserve_topology=False)
        except (ValueError, TypeError, AttributeError):
            continue
        if line.is_empty:
            continue
        segment_id = str(p["id"])
        ids.append(segment_id)
        simplified = mapping(line)
        out.append({
            "type": "Feature",
            "id": segment_id,
            "geometry": {"type": simplified["type"], "coordinates": _round(simplified["coordinates"])},
            "properties": {
                "id": segment_id,
                "name": p.get("nameId") or p.get("name"),
                "description": p.get("name"),
                "route": p.get("routeName"),
            },
        })
    out.sort(key=lambda f: f["id"])
    raw = json.dumps({"type": "FeatureCollection", "features": out}, separators=(",", ":")).encode()
    return gzip.compress(raw, mtime=0), sorted(ids)


# ── one feed ──────────────────────────────────────────────────────────────────

def _load(store: Store, region: str, feed: str) -> dict | None:
    raw = store.get(roads.key(region, feed))
    return json.loads(raw) if raw else None


def _put(store: Store, region: str, feed: str, doc: dict) -> None:
    store.put(roads.key(region, feed), json.dumps(doc, indent=1).encode(), "application/json")


async def refresh_feed(
    client: httpx.AsyncClient, store: Store, region: str, feed: str,
    now: datetime, force: bool = False,
) -> bool:
    """Returns True if the feed failed."""
    old = _load(store, region, feed)
    due = not old or force or not roads.INTERVAL[feed]
    if not due and now - datetime.fromisoformat(old["fetched"]) < roads.INTERVAL[feed] - SLACK:
        return False
    try:
        if feed == "conditions":
            features = await fetch_feed(client, roads.UPSTREAM_FEED[feed], skipGeometry="true")
            segments, unmapped = parse_conditions(features)
            previous = (old or {}).get("count", 0)
            if not segments:
                raise BadFile("no segments")
            if len(segments) < MIN_KEEP_SHARE * previous:
                raise BadFile(f"{len(segments)} segments, the served file has {previous}")
            body: dict = {"count": len(segments), "segments": segments}
        else:
            features = await fetch_feed(client, roads.UPSTREAM_FEED[feed])
            events, unmapped = parse_events(features, feed, now)
            if feed == "planned" and not events:
                raise BadFile("no planned events")
            body = {"count": len(events), "events": events}
    except Exception as exc:  # noqa: BLE001 - one feed must never stop the others
        logger.error("%s %s: refresh failed, keeping the served file: %s", region, feed, describe(exc))
        return True
    if unmapped:
        logger.info("%s %s: shown as other (CDOT's text): %s", region, feed, dict(unmapped.most_common(20)))
    doc = {"region": region, "feed": feed, "fetched": iso(now), **body, "unmapped": dict(unmapped)}
    _put(store, region, feed, doc)
    logger.info("%s %s: %d", region, feed, doc["count"])
    return False


async def refresh_segments(
    client: httpx.AsyncClient, store: Store, region: str, now: datetime, force: bool = False,
) -> bool:
    """The segment lines, daily or when conditions name a segment the lines
    don't have. Returns True if they failed."""
    pointer = _load(store, region, "segments")
    if pointer and not force:
        conditions = _load(store, region, "conditions") or {}
        missing = set(conditions.get("segments", {})) - set(pointer.get("ids", []))
        age = now - datetime.fromisoformat(pointer["fetched"])
        if age < roads.SEGMENTS_INTERVAL - SLACK and not missing:
            return False
    try:
        features = await fetch_feed(client, roads.UPSTREAM_FEED["conditions"])
        body, ids = await asyncio.to_thread(build_segments, features)
        previous = len((pointer or {}).get("ids", []))
        if not ids:
            raise BadFile("no segment lines")
        if len(ids) < MIN_KEEP_SHARE * previous:
            raise BadFile(f"{len(ids)} segment lines, the served file has {previous}")
    except Exception as exc:  # noqa: BLE001
        logger.error("%s segments: refresh failed, keeping the served lines: %s", region, describe(exc))
        return True
    sha = hashlib.sha256(body).hexdigest()[:8]
    revision = (pointer or {}).get("revision")
    if not revision or not revision.endswith(sha):
        revision = f"{now:%Y%m%d}-{sha}"
        store.put(roads.segments_key(region, revision), body, "application/geo+json")
    _put(store, region, "segments", {
        "region": region, "feed": "segments", "fetched": iso(now),
        "revision": revision, "count": len(ids), "bytes": len(body), "ids": ids,
    })
    removed = cleanup(
        store, f"roads/{region}/segments/", {roads.segments_key(region, revision)},
        now - KEEP_SEGMENT_REVISIONS, suffix=".geojson.gz",
    )
    logger.info("%s segments: %d lines, %d bytes, revision %s (%d old removed)",
                region, len(ids), len(body), revision, removed)
    return False


async def run(
    store: Store, client: httpx.AsyncClient, regions: list[str],
    feeds: list[str] | None = None, now: datetime | None = None, force: bool = False,
) -> bool:
    """Refresh every feed that is due. True if all went well."""
    if not roads.configured():
        logger.info("no COTRIP_API_KEY: road conditions are off, nothing fetched")
        return True
    now = now or datetime.now(UTC)
    ok = True
    for region in regions:
        for feed in feeds or (*roads.FEEDS, "segments"):
            try:
                if feed == "segments":
                    failed = await refresh_segments(client, store, region, now, force)
                else:
                    failed = await refresh_feed(client, store, region, feed, now, force)
            except Exception as exc:  # noqa: BLE001 - MinIO; the next feed still runs
                logger.error("%s %s: refresh failed: %s", region, feed, describe(exc))
                failed = True
            ok = ok and not failed
    return ok


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO, key included.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log_redact.install()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": get_settings().weather_user_agent}, timeout=60.0,
    )


async def _main(feeds: list[str] | None, dry_run: bool, force: bool) -> bool:
    store: Store = DryRunStore(S3Store()) if dry_run else S3Store()
    async with client() as http:
        return await run(store, http, list(roads.REGIONS), feeds, force=force)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--feed", choices=(*roads.FEEDS, "segments"), action="append",
                        help="default: every feed")
    parser.add_argument("--force", action="store_true",
                        help="ask every feed now, even if its file is recent")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and check, but write nothing to MinIO")
    args = parser.parse_args()
    setup_logging()
    try:
        ok = asyncio.run(_main(args.feed, args.dry_run, args.force))
    except Exception as exc:  # noqa: BLE001 - never let a traceback print a URL
        logger.error("roads refresh failed: %s", describe(exc))
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
