"""Road conditions (CDOT COtrip data feeds): shared pieces.

The 5-minute job (app/jobs/roads_refresh.py) reads three keyed feeds from
https://data.cotrip.org/api/v1 and writes one file per feed and region,
each only when that feed answered in full and passed the checks:

    dem-cogs/roads/<region>/conditions.json     roadConditions, per segment (every run)
    dem-cogs/roads/<region>/incidents.json      incidents (every run)
    dem-cogs/roads/<region>/planned.json        plannedEvents (every 30 min)
    dem-cogs/roads/<region>/segments.json       which segment lines are served
    dem-cogs/roads/<region>/segments/<rev>.geojson.gz   the lines (daily)

GET /roads (routers/roads.py) serves the first three plus the disclaimer;
GET /tiles/roads/segments/<rev>.geojson serves the lines. docs/runbook.md §11.

Classification: only values seen in the real data map to a category, and
everything else is "other" with CDOT's own text, so the app shows exactly
what CDOT says. Chain law, traction law, avalanche control and crashes have
categories but nothing maps to them until real values have been recorded
(make roads-sample). Never map a value from a guess about its wording.

The terms require DISCLAIMER wherever the data is shown. CDOT may revoke
access at any time; an empty COTRIP_API_KEY turns the layer off.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings
from app.http_retry import call_with_resilience
from app.services import rasterlayer

API = "https://data.cotrip.org/api/v1"

# CDOT only covers Colorado.
REGIONS = ("colorado",)
FEEDS = ("conditions", "incidents", "planned")
# The COtrip feed name for each of ours.
UPSTREAM_FEED = {"conditions": "roadConditions", "incidents": "incidents", "planned": "plannedEvents"}

# How often the job asks each feed (0: every run), when a feed is stale, and
# when it is dropped: an old "open" or "dry" is worse than none.
INTERVAL = {"conditions": timedelta(0), "incidents": timedelta(0), "planned": timedelta(minutes=30)}
STALE_AFTER = {
    "conditions": timedelta(minutes=30),
    "incidents": timedelta(minutes=30),
    "planned": timedelta(hours=2),
}
MAX_AGE = timedelta(hours=24)
SEGMENTS_INTERVAL = timedelta(hours=24)
SEGMENTS_STALE_AFTER = timedelta(days=3)

# Verbatim from the COtrip data feed terms of service (manage-api.cotrip.org,
# read Oct 16, 2026): "Applications using data supplied by this site must
# include the following disclaimers on their sites".
DISCLAIMER = (
    "The data made available here has been modified for use from its original source, "
    "which is the State of Colorado, Department of Transportation (CDOT). "
    "THE STATE OF COLORADO AND CDOT MAKES NO REPRESENTATIONS OR WARRANTY AS TO THE "
    "COMPLETENESS, ACCURACY, TIMELINESS, OR CONTENT OF ANY DATA MADE AVAILABLE THROUGH "
    "THIS SITE. THE STATE OF COLORADO AND CDOT EXPRESSLY DISCLAIMS ALL WARRANTIES, WHETHER "
    "EXPRESS OR IMPLIED, INCLUDING ANY IMPLIED WARRANTIES OF MERCHANTABILITY, OR FITNESS "
    "FOR A PARTICULAR PURPOSE. The data is subject to change as modifications and updates "
    "are complete. It is understood that the information contained in the Web feed is "
    "being used at one's own risk."
)
ATTRIBUTION = {
    "text": "Colorado Department of Transportation (CDOT), COtrip",
    "links": [{"text": "cotrip.org", "url": "https://www.cotrip.org/"}],
}

CATEGORIES = (
    "closed", "chain_law", "traction_law", "avalanche_control", "crash", "construction",
    "dry", "no_data", "other",
)

# ── road conditions ───────────────────────────────────────────────────────────
# conditionDescription (lower case) -> category. Seen in the real feed on
# Oct 18, 2026 (a dry day): only these three, plus the NDFD forecast entry.
CONDITIONS = {
    "3 - dry": "dry",
    "seasonal closure": "closed",
    "no data": "no_data",
}
# The NDFD entry on every segment: forecast text in additionalData, not a
# road condition.
FORECAST = "forecast text included"
# A segment shows its most important condition. Unknown text ("other")
# ranks above dry, so a new CDOT condition is never hidden by "dry".
SEGMENT_ORDER = ("closed", "avalanche_control", "chain_law", "traction_law", "other", "dry", "no_data")


def condition_category(description: str) -> str:
    return CONDITIONS.get(description.strip().lower(), "other")


def segment_category(categories: list[str]) -> str:
    present = [c for c in SEGMENT_ORDER if c in categories]
    return present[0] if present else "no_data"


# ── incidents and planned events ──────────────────────────────────────────────
# Incident/planned `type` -> category. Empty: the real feed on Oct 18 had
# no crash, chain law, traction law or avalanche types. Fill it from the
# roads-sample week.
EVENT_TYPES: dict[str, str] = {}
# CDOT's own `category` field. Every planned event on Oct 18 had one of
# Construction, Maintenance, ITS Work, Utility, Traffic; only Construction
# is a category of ours.
EVENT_CATEGORIES = {"construction": "construction"}


def decode_lanes(impact: dict) -> dict:
    """One laneImpacts entry. laneClosures is a 16-bit mask as hex, most
    significant bit first: bit 0 the left shoulder, bits 1..laneCount the
    lanes from the median out, bit 15 the right shoulder (user guide v1.6,
    appendix "Lane Impact and Lane Configuration"). `all_closed` is every
    lane in that direction closed, the guide's full-closure table."""
    count = impact.get("laneCount")
    try:
        mask = int(str(impact.get("laneClosures") or "0"), 16)
        count = int(count)
    except (TypeError, ValueError):
        mask, count = None, None
    closed = None
    if mask is not None and count is not None and 0 <= count <= 14:
        closed = sum(1 for i in range(1, count + 1) if mask & (0x8000 >> i))
    return {
        "direction": impact.get("direction"),
        "lane_count": count,
        "lanes_closed": closed,
        "all_closed": bool(count) and closed == count,
        "closed_types": list(impact.get("closedLaneTypes") or []),
    }


def event_category(props: dict, lanes: list[dict]) -> str:
    """closed when every direction listed has every lane closed (at least
    one direction, each with a lane count); else by `type`, then by CDOT's
    `category`; else other."""
    if lanes and all(lane["all_closed"] for lane in lanes):
        return "closed"
    if mapped := EVENT_TYPES.get((props.get("type") or "").strip()):
        return mapped
    return EVENT_CATEGORIES.get((props.get("category") or "").strip().lower(), "other")


# ── times ─────────────────────────────────────────────────────────────────────

def ms_time(value) -> str | None:
    """roadConditions times are epoch milliseconds; an open end is far in
    the future (year 2126)."""
    try:
        dt = datetime.fromtimestamp(float(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return None if dt.year > 2100 else rasterlayer.iso(dt)


def iso_time(value) -> str | None:
    """incidents/plannedEvents times are ISO 8601 ("...T11:43:04.082Z")."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return rasterlayer.iso(dt)


def active(event: dict, now: datetime) -> bool | None:
    """Whether the event is in effect now. Planned: inside one of its
    schedule windows (None if a window repeats on days of the week, which
    the feed hasn't used). Incidents: started and not cleared."""
    def t(value):
        return datetime.fromisoformat(value) if value else None

    if event["feed"] == "planned" and event.get("schedule"):
        if any(window.get("days") for window in event["schedule"]):
            return None
        return any(
            (t(w["start"]) or now) <= now < (t(w["end"]) or now + timedelta(seconds=1))
            for w in event["schedule"]
        )
    start, end = t(event.get("start")), t(event.get("end"))
    return (start is None or start <= now) and (end is None or now < end)


# ── storage ───────────────────────────────────────────────────────────────────

REVISION = re.compile(r"^\d{8}-[0-9a-f]{8}$")


def configured() -> bool:
    return bool(get_settings().cotrip_api_key)


def key(region: str, feed: str) -> str:
    return f"roads/{region}/{feed}.json"


def segments_key(region: str, revision: str) -> str:
    return f"roads/{region}/segments/{revision}.geojson.gz"


_docs: dict[str, tuple[dict | None, float]] = {}


async def read_doc(region: str, feed: str) -> dict | None:
    """The stored file for one feed (or "segments", the lines pointer), or
    None if the job hasn't written it. Raises if MinIO can't be read."""
    return await rasterlayer.read_pointer(key(region, feed), _docs)


def state(doc: dict | None, feed: str, now: datetime) -> str:
    if doc is None:
        return "missing"
    age = now - datetime.fromisoformat(doc["fetched"])
    if age > MAX_AGE:
        return "expired"
    if age > STALE_AFTER[feed]:
        return "stale"
    return "ok"


_lines: OrderedDict[str, bytes] = OrderedDict()
_LINES_MAX = 4
_HTTP = httpx.AsyncClient(timeout=10.0)


async def read_segments(region: str, revision: str) -> bytes | None:
    """The gzipped segment lines for one revision, or None if there is no
    such file. Never changes once written, so kept per worker."""
    k = segments_key(region, revision)
    if k in _lines:
        _lines.move_to_end(k)
        return _lines[k]
    settings = get_settings()
    url = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{k}"

    async def _do() -> httpx.Response:
        r = await _HTTP.get(url)
        if r.status_code not in (403, 404):
            r.raise_for_status()
        return r

    r = await call_with_resilience("minio", _do)
    if r.status_code in (403, 404):
        return None
    body = r.content
    _lines[k] = body
    while len(_lines) > _LINES_MAX:
        _lines.popitem(last=False)
    return body
