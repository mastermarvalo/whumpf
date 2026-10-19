"""Road conditions: the refresh job (app/jobs/roads_refresh.py), the sampler
(app/jobs/roads_sample.py), GET /roads and /tiles/roads/segments
(routers/roads.py), the /status item and the key-redacting log filter.
COtrip and MinIO are faked; the records copy the shapes of the real feeds
(Oct 18, 2026)."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from limits import parse
from tenacity import wait_none

from app import http_retry, log_redact, tile_tokens
from app.config import get_settings
from app.jobs import roads_refresh as job
from app.jobs import roads_sample
from app.main import app
from app.routers import status
from app.services import rasterlayer, roads
from app.services.rasterlayer import DryRunStore
from tests.weather_fakes import FakeStore, reset_breakers

NOW = datetime(2026, 10, 19, 2, 0, tzinfo=UTC)
KEY = "cotrip-secret-key-0123456789abcd"
MS = 1792184154750  # an epoch-ms time from the feed (2026-10-16)
OPEN_END = 4945928627814  # roadConditions' "no end" (year 2126)

# ── fake COtrip ───────────────────────────────────────────────────────────────


def condition(cid, text, source="OPERATOR", data=None):
    return {"id": cid, "conditionId": cid, "conditionDescription": text, "startTime": MS,
            "endTime": OPEN_END, "updateTime": MS, "sourceType": source, "additionalData": data}


def forecast():
    return condition(47, "forecast text included", "NDFD", "Mostly cloudy. Wind gusts of up to 10 mph are expected.")


def segment(sid, name, *conds):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [
            [-105.0 - i * 0.00001, 39.0 + i * 0.0001] for i in range(200)
        ]},
        "properties": {"id": str(sid), "name": f"{name}, between A and B", "nameId": name,
                       "routeName": "US 34", "currentConditions": [*conds, forecast()]},
    }


def lanes(direction, count, mask, *types):
    return {"direction": direction, "laneCount": count, "laneClosures": mask, "closedLaneTypes": list(types)}


def event(eid, type_, category, impacts, *, clear=None, start="2026-10-18T13:00:00Z", extra=None, **kw):
    props = {
        "id": eid, "type": type_, "category": category, "routeName": "CO-46E", "direction": "east",
        "travelerInformationMessage": f"{type_} between X and Y.", "laneImpacts": impacts,
        "startTime": start, "lastUpdated": "2026-10-12T19:39:29.465Z",
        "additionalImpacts": kw.pop("additional", None),
    }
    if clear:
        props["clearTime"] = clear
        props["schedule"] = [{"startTime": start, "endTime": clear}]
    props.update(extra or {})
    return {"type": "Feature", "geometry": {"srid": 4326, "type": "MultiPoint",
            "coordinates": [[-105.398244, 39.816103], [-105.397338, 39.815824]]}, "properties": props}


class Upstream:
    """data.cotrip.org. `down` holds feed names that answer 500; `page`
    is the page size."""

    def __init__(self) -> None:
        self.feeds = {
            "roadConditions": [
                segment(1, "Trail Ridge Road", condition(60, "no data")),
                segment(2, "Mt. Evans", condition(59, "seasonal closure")),
                segment(3, "Vail Pass", condition(17, "3 - dry")),
                segment(4, "Berthoud Pass", condition(17, "3 - dry"), condition(99, "Icy Spots")),
            ],
            "incidents": [
                event("OpenTMS-Incident1", "Maintenance Operations", "Traffic",
                      [lanes("east", 1, "0"), lanes("west", 1, "4000", "through lanes")],
                      additional=["Alternating Traffic"]),
            ],
            "plannedEvents": [
                # Both directions, every lane: closed (CDOT: "Road closed").
                event("OpenTMS-Event1", "Rock Work", "Maintenance",
                      [lanes("east", 1, "4000", "through lanes"), lanes("west", 1, "C001", "through lanes")],
                      clear="2026-10-22T22:00:00Z", start="2026-10-19T13:00:00Z"),
                event("OpenTMS-Event2", "Paving Operations", "Construction",
                      [lanes("east", 2, "2000", "right lane"), lanes("west", 2, "0")],
                      clear="2026-10-20T01:00:00Z", start="2026-10-18T13:00:00Z",
                      additional=["Alternating traffic", "Alternating Lanes"]),
                # One direction fully closed only.
                event("OpenTMS-Event3", "Road Construction", "Construction",
                      [lanes("west", 2, "6000", "left lane", "right lane"), lanes("east", 2, "0")],
                      clear="2026-10-21T11:00:00Z", start="2026-10-20T03:00:00Z"),
                event("OpenTMS-Event4", "IT or Fiber Optics", "ITS Work", [lanes("east", 0, "0")],
                      clear="2026-10-25T00:00:00Z"),
                # Already over: left out.
                event("OpenTMS-Event5", "Bridge Repair", "Construction", [], clear="2026-10-18T00:00:00Z"),
            ],
        }
        self.page = 2
        self.down: set[str] = set()
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "data.cotrip.org":
            return httpx.Response(404)
        feed = request.url.path.rsplit("/", 1)[-1]
        if request.url.params.get("apiKey") != KEY:
            return httpx.Response(403, json={"message": "Not Authorized"})
        if feed in self.down or feed not in self.feeds:
            return httpx.Response(500, text="Internal Server Error")
        if request.url.params.get("offset") == "0":
            return httpx.Response(500, text="Internal Server Error")  # as the real API
        start = int(request.url.params.get("offset", "c0")[1:])
        items = self.feeds[feed][start:start + self.page]
        if feed == "roadConditions" and request.url.params.get("skipGeometry") == "true":
            items = [{**f, "geometry": {}} for f in items]
        more = start + self.page < len(self.feeds[feed])
        cursor = f"c{start + self.page}"
        if feed == "roadConditions":
            cursor = f"b'{cursor}'"  # COtrip sends a bytes repr for this feed
        headers = {"Next-Offset": cursor if more else "None", "Page-Size": str(len(items))}
        return httpx.Response(200, json={"type": "FeatureCollection", "features": items}, headers=headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def calls(self, feed: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/" + feed)]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    reset_breakers(monkeypatch, "cotrip")
    monkeypatch.setattr(get_settings(), "cotrip_api_key", KEY)
    monkeypatch.setattr(roads, "_docs", {})
    yield
    http_retry._BREAKERS.pop("cotrip", None)


@pytest.fixture
def up() -> Upstream:
    return Upstream()


def _run(store, up, now=NOW, **kwargs) -> bool:
    async def go():
        async with up.client() as client:
            return await job.run(store, client, ["colorado"], now=now, **kwargs)
    return asyncio.run(go())


# ── classification ────────────────────────────────────────────────────────────

def test_conditions_map_only_what_the_feed_has_shown():
    assert roads.condition_category("3 - dry") == "dry"
    assert roads.condition_category(" Seasonal Closure ") == "closed"
    assert roads.condition_category("no data") == "no_data"
    for text in ("chain law in effect", "traction law", "closed", "icy", "4 - wet", ""):
        assert roads.condition_category(text) == "other"
    assert roads.segment_category(["dry", "other"]) == "other"
    assert roads.segment_category(["dry", "closed", "other"]) == "closed"
    assert roads.segment_category([]) == "no_data"


@pytest.mark.parametrize(("impact", "closed", "all_closed"), [
    (lanes("east", 1, "4000", "through lanes"), 1, True),
    (lanes("east", 1, "C001"), 1, True),       # guide table A-3, shoulders closed too
    (lanes("east", 3, "7000"), 3, True),
    (lanes("east", 3, "F001"), 3, True),
    (lanes("east", 3, "e000"), 2, False),      # left shoulder + lanes 1-2
    (lanes("east", 2, "2000", "right lane"), 1, False),
    (lanes("east", 2, "8001"), 0, False),      # both shoulders only
    (lanes("east", 0, "0"), 0, False),
    (lanes("east", 2, "zz"), None, False),
    (lanes("east", None, "4000"), None, False),
])
def test_lanes_follow_the_guide_bitmask(impact, closed, all_closed):
    lane = roads.decode_lanes(impact)
    assert lane["lanes_closed"] == closed and lane["all_closed"] is all_closed


def test_closed_needs_every_lane_in_every_direction():
    one_way = [roads.decode_lanes(lanes("west", 1, "4000")), roads.decode_lanes(lanes("east", 1, "0"))]
    both = [roads.decode_lanes(lanes("west", 1, "4000")), roads.decode_lanes(lanes("east", 1, "C001"))]
    assert roads.event_category({"category": "Traffic"}, one_way) == "other"
    assert roads.event_category({"category": "Traffic"}, both) == "closed"
    assert roads.event_category({"category": "Construction"}, []) == "construction"
    for props in ({"category": "Maintenance", "type": "Rock Work"}, {"type": "1 Vehicle Crash"},
                  {"type": "Chain Law"}, {"type": "Avalanche Control"}, {}):
        assert roads.event_category(props, []) == "other"


def test_times():
    assert roads.ms_time(MS) == "2026-10-16T20:55:54Z"
    assert roads.ms_time(OPEN_END) is None and roads.ms_time(None) is None
    assert roads.iso_time("2026-10-09T11:43:04.082Z") == "2026-10-09T11:43:04Z"
    assert roads.iso_time("junk") is None


def test_next_offset():
    assert job.next_offset("b'Cn0Sd2=='") == "Cn0Sd2=="
    assert job.next_offset("Cn0Sd2==") == "Cn0Sd2=="
    for last in (None, "", "None", "b'None'", "null"):
        assert job.next_offset(last) is None


# ── the job ───────────────────────────────────────────────────────────────────

def test_refresh_writes_each_feed(up):
    store = FakeStore(NOW)
    assert _run(store, up) is True

    cond = store.json("roads/colorado/conditions.json")
    assert cond["fetched"] == "2026-10-19T02:00:00Z" and cond["count"] == 4
    segs = cond["segments"]
    assert {k: v["category"] for k, v in segs.items()} == {"1": "no_data", "2": "closed", "3": "dry", "4": "other"}
    berthoud = segs["4"]
    assert berthoud["name"] == "Berthoud Pass" and berthoud["route"] == "US 34"
    assert [(c["category"], c["text"]) for c in berthoud["conditions"]] == [("dry", "3 - dry"), ("other", "Icy Spots")]
    assert berthoud["conditions"][0]["end"] is None
    assert berthoud["forecast"]["text"].startswith("Mostly cloudy")
    assert cond["unmapped"] == {"Icy Spots": 1}

    planned = store.json("roads/colorado/planned.json")
    by_id = {e["id"]: e for e in planned["events"]}
    assert set(by_id) == {"OpenTMS-Event1", "OpenTMS-Event2", "OpenTMS-Event3", "OpenTMS-Event4"}
    assert by_id["OpenTMS-Event1"]["category"] == "closed"
    assert by_id["OpenTMS-Event2"]["category"] == "construction"
    assert by_id["OpenTMS-Event2"]["impacts"] == ["Alternating traffic", "Alternating Lanes"]
    three = by_id["OpenTMS-Event3"]
    assert three["category"] == "construction" and three["closed_directions"] == ["west"]
    assert by_id["OpenTMS-Event4"]["category"] == "other" and by_id["OpenTMS-Event4"]["type"] == "IT or Fiber Optics"
    assert planned["unmapped"] == {"IT or Fiber Optics": 1}
    assert by_id["OpenTMS-Event1"]["geometry"]["type"] == "MultiPoint"
    assert "srid" not in by_id["OpenTMS-Event1"]["geometry"]

    incidents = store.json("roads/colorado/incidents.json")
    (incident,) = incidents["events"]
    assert incident["category"] == "other" and incident["closed_directions"] == ["west"]

    pointer = store.json("roads/colorado/segments.json")
    assert pointer["ids"] == ["1", "2", "3", "4"]
    lines = json.loads(gzip.decompress(store.get(roads.segments_key("colorado", pointer["revision"]))))
    assert [f["id"] for f in lines["features"]] == ["1", "2", "3", "4"]
    assert lines["features"][0]["properties"]["name"] == "Trail Ridge Road"
    # 200 points on a straight line simplify to its two ends.
    assert len(lines["features"][0]["geometry"]["coordinates"]) == 2


def test_pages_are_followed_with_the_bytes_wrapped_cursor(up):
    _run(FakeStore(NOW), up)
    offsets = [r.url.params.get("offset") for r in up.calls("roadConditions")]
    assert offsets == [None, "c2"] * 2  # conditions, then the lines
    assert "offset" not in up.calls("incidents")[0].url.params  # offset=0 breaks the real API
    assert all(r.url.params["skipGeometry"] == "true" for r in up.calls("roadConditions")[:2])


def test_repeating_cursor_fails_the_feed(up, monkeypatch):
    monkeypatch.setattr(job, "next_offset", lambda header: "same")
    store = FakeStore(NOW)
    assert _run(store, up, feeds=["incidents"]) is False
    assert store.puts == []


def test_one_feed_down_keeps_its_file_and_the_others_refresh(up):
    store = FakeStore(NOW)
    _run(store, up)
    up.down.add("incidents")
    later = NOW + timedelta(minutes=5)
    assert _run(store, up, now=later) is False
    assert store.json("roads/colorado/incidents.json")["fetched"] == rasterlayer.iso(NOW)
    assert store.json("roads/colorado/conditions.json")["fetched"] == rasterlayer.iso(later)


def test_conditions_that_shrink_by_half_are_not_written(up):
    store = FakeStore(NOW)
    _run(store, up)
    up.feeds["roadConditions"] = up.feeds["roadConditions"][:1]
    assert _run(store, up, now=NOW + timedelta(minutes=5), feeds=["conditions"]) is False
    assert store.json("roads/colorado/conditions.json")["count"] == 4
    up.feeds["roadConditions"] = []
    assert _run(store, up, now=NOW + timedelta(minutes=10), feeds=["conditions"]) is False


def test_no_incidents_is_fine_but_no_planned_events_is_not(up):
    store = FakeStore(NOW)
    up.feeds["incidents"] = []
    up.feeds["plannedEvents"] = []
    assert _run(store, up, feeds=["incidents", "planned"]) is False
    assert store.json("roads/colorado/incidents.json")["events"] == []
    assert store.get("roads/colorado/planned.json") is None


def test_planned_every_30_min_and_lines_daily(up):
    store = FakeStore(NOW)
    _run(store, up)
    for minutes in (5, 10):
        _run(store, up, now=NOW + timedelta(minutes=minutes))
    assert len(up.calls("incidents")) == 3
    assert len(up.calls("plannedEvents")) == 3  # one run, 3 pages
    assert len(up.calls("roadConditions")) == 2 + 3 * 2  # lines once, conditions every run
    _run(store, up, now=NOW + timedelta(minutes=30))
    assert len(up.calls("plannedEvents")) == 6


def test_lines_refresh_early_for_a_new_segment_and_keep_their_revision(up):
    store = FakeStore(NOW)
    _run(store, up)
    revision = store.json("roads/colorado/segments.json")["revision"]
    _run(store, up, now=NOW + timedelta(hours=25))
    assert store.json("roads/colorado/segments.json")["revision"] == revision  # same lines

    up.feeds["roadConditions"].append(segment(5, "Kebler Pass", condition(60, "no data")))
    _run(store, up, now=NOW + timedelta(hours=25, minutes=5))
    pointer = store.json("roads/colorado/segments.json")
    assert pointer["ids"][-1] == "5" and pointer["revision"] != revision
    assert pointer["revision"].startswith("20261020-")


def test_without_a_key_nothing_is_fetched(up, monkeypatch):
    monkeypatch.setattr(get_settings(), "cotrip_api_key", "")
    store = FakeStore(NOW)
    assert _run(store, up) is True
    assert up.requests == [] and store.puts == []


def test_dry_run_writes_nothing(up):
    store = FakeStore(NOW)
    assert _run(DryRunStore(store), up) is True
    assert store.puts == []


def test_minio_failing_for_one_feed_still_runs_the_others(up):
    class Flaky(FakeStore):
        def get(self, key):
            if key.endswith("incidents.json"):
                raise OSError("minio down")
            return super().get(key)

    store = Flaky(NOW)
    assert _run(store, up) is False
    assert store.get("roads/colorado/planned.json") is not None


# ── the key never reaches a log ───────────────────────────────────────────────

def test_the_key_never_reaches_the_log(up, caplog):
    """Failing pages, a connection error and httpx's own INFO request lines
    (which carry the URL): the key appears in none of them."""
    caplog.set_level(logging.DEBUG)
    httpx_logger = logging.getLogger("httpx")
    level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    log_redact.install([caplog.handler])
    try:
        up.down |= {"roadConditions", "plannedEvents"}
        assert _run(FakeStore(NOW), up) is False
        assert "refresh failed" in caplog.text and "HTTP 500" in caplog.text

        def refuse(request):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
                return await job.run(FakeStore(NOW), client, ["colorado"], now=NOW)
        assert asyncio.run(go()) is False
        assert "ConnectError" in caplog.text
        assert any("HTTP Request" in r.getMessage() for r in caplog.records)  # httpx did log
        assert KEY not in caplog.text
        assert all(KEY not in r.getMessage() for r in caplog.records)
    finally:
        httpx_logger.setLevel(level)


def test_the_job_itself_never_logs_the_key(up, caplog):
    """Without the filter: the job's own lines are already clean."""
    caplog.set_level(logging.DEBUG, logger="whumpf")
    up.down |= {"roadConditions", "incidents", "plannedEvents"}
    _run(FakeStore(NOW), up)
    ours = [logging.Formatter().format(r) for r in caplog.records if r.name.startswith("whumpf")]
    assert any("refresh failed" in line for line in ours)
    assert not any(KEY in line for line in ours)


def test_redact_filter():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "GET %s failed",
                               (f"https://data.cotrip.org/api/v1/incidents?apiKey={KEY}&offset=c2",), None)
    try:
        raise httpx.ConnectError(f"cannot reach https://data.cotrip.org/api/v1/x?apiKey={KEY}")
    except httpx.ConnectError:
        import sys
        record.exc_info = sys.exc_info()
    record.stack_info = f"stack with {KEY}"
    assert log_redact.RedactFilter().filter(record) is True
    text = logging.Formatter().format(record)
    assert KEY not in text and "apiKey=<redacted>&offset=c2" in text
    assert log_redact.redact(f"key is {KEY}") == "key is <redacted>"
    assert log_redact.redact("APIKEY=abc def") == "APIKEY=<redacted> def"


# ── the sampler ───────────────────────────────────────────────────────────────

def _sample(store, up, now=NOW) -> bool:
    async def go():
        async with up.client() as client:
            return await roads_sample.sample_once(store, client, now=now)
    return asyncio.run(go())


def test_sample_records_distinct_values_with_counts_and_times(up):
    store = FakeStore(NOW)
    assert _sample(store, up) is True
    later = NOW + timedelta(minutes=15)
    up.feeds["incidents"].append(event("OpenTMS-Incident2", "1 Vehicle Crash", "Traffic", [],
                                       additional=["Slower speeds advised"]))
    assert _sample(store, up, now=later) is True
    sample = store.json(roads_sample.KEY)
    assert sample["runs"] == 2 and sample["last_run"] == rasterlayer.iso(later)
    dry = sample["fields"]["conditionDescription"]["3 - dry"]
    assert dry["count"] == 4 and dry["runs"] == 2  # two segments, two runs
    assert dry["first_seen"] == rasterlayer.iso(NOW) and dry["last_seen"] == rasterlayer.iso(later)
    assert dry["conditionId"] == {"17": 4} and dry["sourceType"] == {"OPERATOR": 4} and dry["category"] == {"dry": 4}
    crash = sample["fields"]["incident.type"]["1 Vehicle Crash"]
    assert crash["runs"] == 1 and crash["first_seen"] == rasterlayer.iso(later) and crash["category"] == {"other": 1}
    assert crash["example"] == "1 Vehicle Crash between X and Y."
    assert sample["fields"]["incident.additionalImpacts"]["Alternating Traffic"]["count"] == 2
    assert sample["fields"]["planned.type"]["Rock Work"]["category"] == {"closed": 2}
    report = roads_sample.report(sample)
    assert "'1 Vehicle Crash' -> other" in report and "conditionDescription: 5 distinct" in report


def test_sample_skips_a_failed_feed(up):
    store = FakeStore(NOW)
    up.down.add("incidents")
    assert _sample(store, up) is False
    sample = store.json(roads_sample.KEY)
    assert sample["failed"] == {"incidents": 1} and sample["fields"]["incident.type"] == {}
    assert sample["fields"]["planned.type"]


# ── the API ───────────────────────────────────────────────────────────────────

def _token(tier: str = tile_tokens.TIER_ANON) -> str:
    subject = "i:install-0123456789abcdef" if tier == tile_tokens.TIER_ANON else "u:42"
    return tile_tokens.issue(subject, tier)[0]


@pytest.fixture
def docs(up, monkeypatch) -> dict:
    """docs[feed] = the stored doc (None, or an exception to raise), from a
    real job run, made recent."""
    store = FakeStore(NOW)
    _run(store, up)
    now = datetime.now(UTC)
    state: dict = {}
    for feed in (*roads.FEEDS, "segments"):
        doc = store.json(roads.key("colorado", feed))
        doc["fetched"] = rasterlayer.iso(now - timedelta(minutes=2))
        state[feed] = doc
    # Event windows relative to the real clock: 2 in effect now, 1 and 3
    # later, 4 ended since the job ran.
    windows = {"OpenTMS-Event1": (24, 72), "OpenTMS-Event2": (-1, 1),
               "OpenTMS-Event3": (1, 10), "OpenTMS-Event4": (-5, -1 / 60)}
    for event_ in state["planned"]["events"]:
        start, end = (rasterlayer.iso(now + timedelta(hours=h)) for h in windows[event_["id"]])
        event_.update(start=start, end=end, schedule=[{"start": start, "end": end, "days": None}])
    state["lines"] = store.get(roads.segments_key("colorado", state["segments"]["revision"]))

    async def read(region, feed):
        value = state[feed]
        if isinstance(value, Exception):
            raise value
        return value

    async def read_lines(region, revision):
        return state["lines"] if revision == state["segments"]["revision"] else None

    monkeypatch.setattr(roads, "read_doc", read)
    monkeypatch.setattr(roads, "read_segments", read_lines)
    return state


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _get(client, token="anon", region="colorado"):
    url = f"/roads?region={region}"
    if token:
        url += f"&t={_token() if token == 'anon' else token}"
    return client.get(url)


def test_roads(client, docs):
    resp = _get(client)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=60"
    body = resp.json()
    assert body["disclaimer"] == roads.DISCLAIMER
    assert body["disclaimer"].startswith("The data made available here has been modified")
    assert body["attribution"]["text"].startswith("Colorado Department of Transportation")
    assert {f: s["state"] for f, s in body["sources"].items()} == {"conditions": "ok", "incidents": "ok", "planned": "ok"}
    assert body["conditions"]["2"]["category"] == "closed"
    assert body["conditions"]["4"]["conditions"][1]["text"] == "Icy Spots"
    revision = docs["segments"]["revision"]
    assert body["segments"]["url"] == f"/tiles/roads/segments/{revision}.geojson?region=colorado"
    features = {f["id"]: f for f in body["features"]}
    assert set(features) == {"incidents:OpenTMS-Incident1", "planned:OpenTMS-Event1",
                             "planned:OpenTMS-Event2", "planned:OpenTMS-Event3"}
    assert features["planned:OpenTMS-Event2"]["properties"]["active"] is True
    assert features["planned:OpenTMS-Event3"]["properties"]["active"] is False
    assert features["incidents:OpenTMS-Incident1"]["properties"]["active"] is True
    assert features["planned:OpenTMS-Event1"]["geometry"]["type"] == "MultiPoint"


def test_roads_feeds_fail_on_their_own(client, docs):
    docs["incidents"] = httpx.ConnectError("minio down")
    docs["planned"]["fetched"] = rasterlayer.iso(datetime.now(UTC) - timedelta(hours=3))
    docs["conditions"]["fetched"] = rasterlayer.iso(datetime.now(UTC) - timedelta(hours=25))
    body = _get(client).json()
    assert {f: s["state"] for f, s in body["sources"].items()} == {
        "conditions": "expired", "incidents": "unavailable", "planned": "stale"}
    assert body["conditions"] == {}  # an old "dry" is never shown as current
    assert {f["properties"]["feed"] for f in body["features"]} == {"planned"}
    docs["planned"] = None
    assert _get(client).json()["sources"]["planned"] == {"fetched": None, "state": "missing"}


def test_roads_all_unreadable_is_503(client, docs):
    for feed in roads.FEEDS:
        docs[feed] = httpx.ConnectError("minio down")
    resp = _get(client)
    assert resp.status_code == 503 and resp.headers["cache-control"] == "no-store"


def test_roads_without_a_key_is_off(client, docs, monkeypatch):
    monkeypatch.setattr(get_settings(), "cotrip_api_key", "")
    body = _get(client).json()
    assert body["features"] == [] and body["conditions"] == {} and body["segments"] is None
    assert {s["state"] for s in body["sources"].values()} == {"not_configured"}
    assert body["disclaimer"] == roads.DISCLAIMER


def test_roads_needs_a_valid_tile_token(client, docs, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", True)
    for token in (None, "junk.token"):
        resp = _get(client, token=token)
        assert resp.status_code == 401 and resp.headers["cache-control"] == "no-store"
    assert _get(client, token=_token(tile_tokens.TIER_USER)).status_code == 200


def test_roads_unknown_region_is_400(client, docs):
    assert _get(client, region="atlantis").status_code == 400


def test_roads_rate_limited_per_ip(client, docs, monkeypatch):
    monkeypatch.setitem(tile_tokens.BUCKETS, "roads", (parse("3/minute"),))
    for _ in range(3):
        assert _get(client).status_code == 200
    resp = _get(client)
    assert resp.status_code == 429 and resp.headers["cache-control"] == "no-store"
    url = f"/roads?region=colorado&t={_token()}"
    assert client.get(url, headers={"x-forwarded-for": "203.0.113.9"}).status_code == 200


def test_segments_file(client, docs):
    revision = docs["segments"]["revision"]
    resp = client.get(f"/tiles/roads/segments/{revision}.geojson?region=colorado&t={_token()}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert resp.headers["content-type"] == "application/geo+json"
    assert [f["id"] for f in resp.json()["features"]] == ["1", "2", "3", "4"]  # TestClient un-gzips
    for bad in ("20261019-00000000", "../x", "latest"):
        resp = client.get(f"/tiles/roads/segments/{bad}.geojson?t={_token()}")
        assert resp.status_code == 404


# ── /status ───────────────────────────────────────────────────────────────────

def test_status_roads(docs, monkeypatch):
    assert asyncio.run(status._check_roads()).healthy is True
    docs["incidents"]["fetched"] = rasterlayer.iso(datetime.now(UTC) - timedelta(hours=1))
    health = asyncio.run(status._check_roads())
    assert health.healthy is False and health.detail.startswith("colorado incidents:")
    docs["incidents"] = httpx.ConnectError("minio down")
    assert asyncio.run(status._check_roads()).healthy is False
    monkeypatch.setattr(get_settings(), "cotrip_api_key", "")
    health = asyncio.run(status._check_roads())
    assert health.healthy is True and health.detail == "not configured"
