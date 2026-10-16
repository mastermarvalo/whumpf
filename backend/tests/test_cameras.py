"""Mountain cameras: the refresh job (app/jobs/cameras_refresh.py), GET
/cameras and /cameras/{id}/image (routers/cameras.py) and the /status item.
NPS, Windy, MinIO and the DEM are faked."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from limits import parse
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.jobs import cameras_refresh as job
from app.main import app
from app.routers import status
from app.services import cameras, cog_sampler, rasterlayer
from app.services.rasterlayer import DryRunStore
from tests.weather_fakes import FakeStore, reset_breakers

NOW = datetime(2026, 10, 16, 17, 10, tzinfo=UTC)
NPS_KEY = "nps-secret-key-0123456789"
WINDY_KEY = "windy-secret-key-0123456789"
NPS_CENTRE = (40.3554801940918, -105.69711303710938)

# ── fake upstreams ────────────────────────────────────────────────────────────


def nps_cam(cam_id, title, lat, lon, status_="Active"):
    return {
        "id": cam_id, "title": title, "latitude": lat, "longitude": lon, "status": status_,
        "url": f"https://www.nps.gov/media/webcam/view.htm?id={cam_id}",
        "relatedParks": [{"parkCode": "romo", "fullName": "Rocky Mountain National Park"}],
    }


def nps_page(src: str) -> str:
    return (
        '<div class="Multimedia__WebcamContainer">\n<img alt=""\nclass="Multimedia__Webcam"\n'
        f'id="webcamRefreshImage"\nsrc="{src}" />\n</div>'
    )


def windy_cam(n, lat=39.6, lon=-105.9, status_="active"):
    return {
        "webcamId": 1000 + n, "title": f"Cam {n}", "status": status_,
        "lastUpdatedOn": "2026-10-16T17:00:00.000Z",
        "location": {"latitude": lat, "longitude": lon},
        "urls": {"detail": f"https://www.windy.com/webcams/{1000 + n}"},
    }


class Upstream:
    """NPS API + nps.gov pages + Windy API. `down` holds "nps" / "windy"."""

    def __init__(self) -> None:
        self.nps = [
            nps_cam("FRE", "Fall River Entrance/U.S. Hwy 34", 40.4042, -105.5920),
            nps_cam("GLE", "Grand Lake Entrance/ U.S. Hwy 34", 40.2722, -105.8350),
            # Two cameras on the park-centre point: no position of their own.
            nps_cam("LONGS", "Longs Peak", *NPS_CENTRE),
            nps_cam("81B4686B-1DD8-B71B-0B122CB864D072FC", "Beaver Meadows Entrance", *NPS_CENTRE),
            nps_cam("CD", "Continental Divide", 40.43, -105.75),
            nps_cam("OLD", "Retired", 40.5, -105.6, status_="Inactive"),
        ]
        self.pages = {
            "FRE": "https&#x3a;&#x2f;&#x2f;www.nps.gov&#x2f;webcams-romo&#x2f;fre.jpg",
            "GLE": "https://www.nps.gov/webcams-romo/Grand_Lake_Entrance.jpg",
            "LONGS": "https://www.nps.gov/webcams-romo/longs_peak.jpg",
            "81B4686B-1DD8-B71B-0B122CB864D072FC": "/webcams-romo/bme.jpeg",
            "CD": "/common/commonspot/customcf/webcam/inactive_webcam.png",
        }
        self.windy = [windy_cam(n) for n in range(3)] + [
            windy_cam(90, status_="inactive"),
            windy_cam(91, lat=45.0, lon=-110.0),  # outside the region
        ]
        self.windy_total: int | None = None
        self.down: set[str] = set()
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "developer.nps.gov":
            if "nps" in self.down:
                return httpx.Response(503)
            return httpx.Response(200, json={"total": len(self.nps), "data": self.nps})
        if url.host == "www.nps.gov" and url.path == "/media/webcam/view.htm":
            if "nps" in self.down:
                return httpx.Response(503)
            return httpx.Response(200, text=nps_page(self.pages[url.params["id"]]))
        if url.host == "www.nps.gov" and request.method == "HEAD":
            return httpx.Response(200, headers={"last-modified": "Fri, 16 Oct 2026 17:05:00 GMT"})
        if url.host == "api.windy.com":
            if "windy" in self.down:
                return httpx.Response(500)
            offset, limit = int(url.params["offset"]), int(url.params["limit"])
            total = self.windy_total if self.windy_total is not None else len(self.windy)
            page = self.windy[offset:offset + limit]
            if self.windy_total is not None:
                page = [windy_cam(offset + i) for i in range(min(limit, max(0, total - offset)))]
            return httpx.Response(200, json={"total": total, "webcams": page})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def calls(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    reset_breakers(monkeypatch, "nps", "windy", "nps_image")
    monkeypatch.setattr(get_settings(), "nps_api_key", NPS_KEY)
    monkeypatch.setattr(get_settings(), "windy_webcams_api_key", WINDY_KEY)
    monkeypatch.setattr(cameras, "_images", {})
    monkeypatch.setattr(cameras, "_lists", {})
    yield
    for name in ("nps", "windy", "nps_image"):
        http_retry._BREAKERS.pop(name, None)


@pytest.fixture
def dem(monkeypatch) -> list:
    calls: list = []

    def sample(points, region, settings):
        calls.append(list(points))
        return [3000.4 if lon > -106 else None for lon, _ in points]

    monkeypatch.setattr(cog_sampler, "sample_elevations", sample)
    return calls


@pytest.fixture
def up() -> Upstream:
    return Upstream()


def _run(store, up, now=NOW, **kwargs) -> bool:
    async def go():
        async with up.client() as client:
            return await job.run(store, client, ["colorado"], now=now, **kwargs)
    return asyncio.run(go())


# ── the job ───────────────────────────────────────────────────────────────────

def test_nps_list(up, dem):
    store = FakeStore(NOW)
    assert _run(store, up, sources=["nps"]) is True
    doc = store.json("cameras/colorado/nps.json")
    assert doc["fetched"] == "2026-10-16T17:10:00Z" and doc["count"] == 3
    by_id = {c["id"]: c for c in doc["cameras"]}
    # Longs Peak has only the shared park-centre point and no hand-set one;
    # Continental Divide shows the inactive image; "Retired" is Inactive.
    assert set(by_id) == {"nps:FRE", "nps:GLE", "nps:81B4686B-1DD8-B71B-0B122CB864D072FC"}
    fre = by_id["nps:FRE"]
    assert fre["image_url"] == "https://www.nps.gov/webcams-romo/fre.jpg"  # entities decoded
    assert (fre["lon"], fre["lat"]) == (-105.592, 40.4042)
    assert fre["updated"] == "2026-10-16T17:05:00Z"
    assert fre["credit"] == "NPS / Rocky Mountain National Park"
    assert fre["link"] == "https://www.nps.gov/media/webcam/view.htm?id=FRE"
    assert fre["elevation_m"] == 3000
    bme = by_id["nps:81B4686B-1DD8-B71B-0B122CB864D072FC"]
    assert (bme["lon"], bme["lat"]) == job.NPS_POSITIONS["81B4686B-1DD8-B71B-0B122CB864D072FC"]
    assert bme["image_url"] == "https://www.nps.gov/webcams-romo/bme.jpeg"  # relative resolved
    api = up.calls("developer.nps.gov")[0]
    assert api.headers["x-api-key"] == NPS_KEY and NPS_KEY not in str(api.url)
    assert api.url.params["parkCode"] == "romo"


def test_nps_image_url():
    page = "https://www.nps.gov/media/webcam/view.htm?id=X"
    assert job.nps_image_url(page, nps_page("/webcams-romo/a.jpg")) == "https://www.nps.gov/webcams-romo/a.jpg"
    assert job.nps_image_url(page, nps_page("/x/inactive_webcam.png")) is None
    assert job.nps_image_url(page, "<img id='other' src='/a.jpg'>") is None
    assert job.nps_image_url(page, nps_page("http://example.com/a.jpg")) is None


def test_windy_list_pages_through_the_region(up, dem, monkeypatch):
    monkeypatch.setattr(job, "WINDY_PAGE", 2)
    store = FakeStore(NOW)
    assert _run(store, up, sources=["windy"]) is True
    doc = store.json("cameras/colorado/windy.json")
    assert [c["id"] for c in doc["cameras"]] == ["windy:1000", "windy:1001", "windy:1002"]
    cam = doc["cameras"][0]
    assert cam["image_url"] is None and cam["link"] == "https://www.windy.com/webcams/1000"
    assert cam["credit"] == "windy.com" and cam["updated"] == "2026-10-16T17:00:00.000Z"
    calls = up.calls("api.windy.com")
    assert [r.url.params["offset"] for r in calls] == ["0", "2", "4"]
    west, south, east, north = job.REGIONS["colorado"].max_bounds
    assert calls[0].url.params["bbox"] == f"{north},{east},{south},{west}"
    assert all(r.headers["x-windy-api-key"] == WINDY_KEY for r in calls)
    assert all(WINDY_KEY not in str(r.url) for r in calls)


def test_windy_stops_at_the_free_tier_offset_limit(up, dem, caplog):
    up.windy_total = 5000
    store = FakeStore(NOW)
    assert _run(store, up, sources=["windy"]) is True
    offsets = [int(r.url.params["offset"]) for r in up.calls("api.windy.com")]
    assert offsets[-1] == job.WINDY_MAX_OFFSET and len(offsets) == 21
    assert store.json("cameras/colorado/windy.json")["count"] == 1050
    assert "free tier stops at 1050" in caplog.text


def test_one_source_down_keeps_its_list_and_the_others_refresh(up, dem):
    store = FakeStore(NOW)
    assert _run(store, up) is True
    before = store.get("cameras/colorado/windy.json")
    up.down.add("windy")
    up.nps.append(nps_cam("NEW", "New cam", 40.3, -105.6))
    up.pages["NEW"] = "/webcams-romo/new.jpg"
    later = NOW + timedelta(days=2)
    assert _run(store, up, now=later) is False
    assert store.get("cameras/colorado/windy.json") == before
    assert store.json("cameras/colorado/nps.json")["count"] == 4


def test_a_list_that_shrinks_by_half_or_is_empty_is_not_written(up, dem):
    store = FakeStore(NOW)
    assert _run(store, up, sources=["windy"]) is True
    before = store.get("cameras/colorado/windy.json")
    up.windy = up.windy[:1]
    assert _run(store, up, sources=["windy"], force=True) is False
    up.windy = []
    assert _run(store, up, sources=["windy"], force=True) is False
    assert store.get("cameras/colorado/windy.json") == before


def test_sources_are_asked_only_when_due(up, dem):
    store = FakeStore(NOW)
    assert _run(store, up) is True
    n = len(up.requests)
    assert _run(store, up, now=NOW + timedelta(hours=1)) is True
    assert len(up.requests) == n
    # NPS is due after 6 h, Windy after 24 h.
    assert _run(store, up, now=NOW + timedelta(hours=6)) is True
    assert up.calls("api.windy.com") and len(up.calls("developer.nps.gov")) == 2
    windy_calls = len(up.calls("api.windy.com"))
    assert _run(store, up, now=NOW + timedelta(hours=6), force=True) is True
    assert len(up.calls("api.windy.com")) == 2 * windy_calls


def test_a_source_without_a_key_is_skipped(up, dem, monkeypatch):
    monkeypatch.setattr(get_settings(), "windy_webcams_api_key", "")
    store = FakeStore(NOW)
    assert _run(store, up) is True
    assert up.calls("api.windy.com") == []
    assert store.get("cameras/colorado/windy.json") is None


def test_elevations_are_kept_for_cameras_that_did_not_move(up, dem):
    store = FakeStore(NOW)
    _run(store, up, sources=["windy"])
    assert len(dem) == 1 and len(dem[0]) == 3
    up.windy.append(windy_cam(4, lat=39.7))
    _run(store, up, sources=["windy"], force=True)
    assert dem[1] == [(-105.9, 39.7)]
    assert all(c["elevation_m"] == 3000 for c in store.json("cameras/colorado/windy.json")["cameras"])


def test_minio_failing_for_one_source_still_runs_the_other(up, dem):
    store = FakeStore(NOW)
    real_get = store.get

    def get(key):
        if key.endswith("/nps.json"):
            raise OSError("minio down")
        return real_get(key)

    store.get = get
    assert _run(store, up) is False
    assert store.json("cameras/colorado/windy.json")["count"] == 3


def test_dry_run_writes_nothing(up, dem):
    store = FakeStore(NOW)
    assert _run(DryRunStore(store), up) is True
    assert store.puts == []


def test_keys_never_reach_the_log(up, dem, caplog):
    caplog.set_level(logging.DEBUG)
    up.down |= {"nps", "windy"}
    assert _run(FakeStore(NOW), up) is False
    assert "refresh failed" in caplog.text
    assert NPS_KEY not in caplog.text and WINDY_KEY not in caplog.text


# ── the API ───────────────────────────────────────────────────────────────────

def _token(tier: str = tile_tokens.TIER_ANON) -> str:
    subject = "i:install-0123456789abcdef" if tier == tile_tokens.TIER_ANON else "u:42"
    return tile_tokens.issue(subject, tier)[0]


def _cam(source, sid, **kw):
    cam = {
        "id": f"{source}:{sid}", "name": f"{source} {sid}", "source": source,
        "lon": -105.9, "lat": 39.6, "elevation_m": 3000, "credit": "c", "link": f"https://x/{sid}",
        "image_url": f"https://www.nps.gov/webcams-romo/{sid}.jpg" if source == "nps" else None,
        "updated": "2026-10-16T17:00:00Z",
    }
    return {**cam, **kw}


def _doc(source, cams, age=timedelta(hours=1)):
    return {"source": source, "fetched": rasterlayer.iso(datetime.now(UTC) - age), "cameras": cams}


@pytest.fixture
def lists(monkeypatch) -> dict:
    """lists[source] = the stored doc (None, or an exception to raise)."""
    state: dict = {"nps": _doc("nps", [_cam("nps", "FRE")]), "windy": _doc("windy", [_cam("windy", "1000")])}

    async def read(region, source):
        value = state[source]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(cameras, "read_list", read)
    return state


class FakeHttp:
    def __init__(self) -> None:
        self.windy: object = (200, {
            "webcamId": 1000, "lastUpdatedOn": "2026-10-16T17:08:00.000Z",
            "images": {"current": {"icon": "https://imgproxy.windy.com/i.jpg",
                                   "preview": "https://imgproxy.windy.com/p.jpg?token=abc"}},
        })
        self.head_route: object = (200, {"last-modified": "Fri, 16 Oct 2026 17:09:00 GMT"})
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _answer(route, url):
        if isinstance(route, Exception):
            raise route
        code, body = route
        req = httpx.Request("GET", url)
        if isinstance(body, dict) and "images" in body:
            return httpx.Response(code, json=body, request=req)
        return httpx.Response(code, headers=body, request=req)

    async def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, headers or {}))
        return self._answer(self.windy, url)

    async def head(self, url, **kw):
        self.calls.append((url, {}))
        return self._answer(self.head_route, url)


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(cameras._HTTP, "get", fake.get)
    monkeypatch.setattr(cameras._HTTP, "head", fake.head)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _list(client, token="anon", region="colorado"):
    url = f"/cameras?region={region}"
    if token:
        url += f"&t={_token() if token == 'anon' else token}"
    return client.get(url)


def test_list_merges_every_source(client, lists):
    resp = _list(client)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=300"
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert [f["id"] for f in body["features"]] == ["nps:FRE", "windy:1000"]
    nps, windy = (f["properties"] for f in body["features"])
    assert body["features"][0]["geometry"] == {"type": "Point", "coordinates": [-105.9, 39.6]}
    assert nps["image_url"] == "https://www.nps.gov/webcams-romo/FRE.jpg"
    assert windy["image_url"] is None and windy["image"] == "/cameras/windy:1000/image?region=colorado"
    assert windy["link"] == "https://x/1000" and windy["elevation_m"] == 3000
    assert body["sources"]["nps"]["state"] == body["sources"]["windy"]["state"] == "ok"
    assert "add a webcam" in body["sources"]["windy"]["attribution"]["text"]


def test_one_source_unreadable_or_old_leaves_the_others(client, lists):
    lists["windy"] = httpx.ConnectError("minio down")
    body = _list(client).json()
    assert [f["id"] for f in body["features"]] == ["nps:FRE"]
    assert body["sources"]["windy"]["state"] == "unavailable"

    lists["windy"] = _doc("windy", [_cam("windy", "1")], age=timedelta(days=4))
    lists["nps"] = _doc("nps", [_cam("nps", "FRE")], age=timedelta(days=8))
    body = _list(client).json()
    assert [f["id"] for f in body["features"]] == ["windy:1"]
    assert body["sources"]["windy"]["state"] == "stale"
    assert body["sources"]["nps"]["state"] == "expired"

    lists["nps"] = None
    assert _list(client).json()["sources"]["nps"] == {
        "attribution": cameras.ATTRIBUTION["nps"], "fetched": None, "state": "missing",
    }


def test_every_source_unreadable_is_503(client, lists):
    lists["nps"] = lists["windy"] = httpx.ConnectError("minio down")
    resp = _list(client)
    assert resp.status_code == 503 and resp.headers["cache-control"] == "no-store"


def test_a_source_without_a_key_is_not_served(client, lists, monkeypatch):
    monkeypatch.setattr(get_settings(), "windy_webcams_api_key", "")
    body = _list(client).json()
    assert [f["id"] for f in body["features"]] == ["nps:FRE"]
    assert body["sources"]["windy"]["state"] == "not_configured"
    assert client.get(f"/cameras/windy:1000/image?t={_token()}").status_code == 404


def test_needs_a_valid_tile_token(client, lists, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", True)
    for token in (None, "junk.token"):
        resp = _list(client, token=token)
        assert resp.status_code == 401 and resp.headers["cache-control"] == "no-store"
    assert client.get("/cameras/nps:FRE/image").status_code == 401
    assert _list(client, token=_token(tile_tokens.TIER_USER)).status_code == 200


def test_unknown_region_is_400(client, lists):
    assert _list(client, region="atlantis").status_code == 400


def test_rate_limited_per_ip(client, lists, monkeypatch):
    monkeypatch.setitem(tile_tokens.BUCKETS, "cameras", (parse("3/minute"),))
    for _ in range(3):
        assert _list(client).status_code == 200
    resp = _list(client)
    assert resp.status_code == 429 and resp.headers["cache-control"] == "no-store"
    assert _list(client, token=_token()).status_code == 429
    url = f"/cameras?region=colorado&t={_token()}"
    assert client.get(url, headers={"x-forwarded-for": "203.0.113.9"}).status_code == 200


def _image(client, camera_id):
    return client.get(f"/cameras/{camera_id}/image?region=colorado&t={_token()}")


def test_nps_image_is_the_stored_url_with_its_age(client, lists, http):
    resp = _image(client, "nps:FRE")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, max-age=60"
    assert resp.json() == {
        "id": "nps:FRE", "url": "https://www.nps.gov/webcams-romo/FRE.jpg",
        "taken": "2026-10-16T17:09:00Z", "expires": None, "credit": "c", "link": "https://x/FRE",
    }
    cameras._images.clear()
    http.head_route = httpx.ConnectError("nps.gov down")
    body = _image(client, "nps:FRE").json()
    assert body["taken"] == "2026-10-16T17:00:00Z"  # what the job saw


def test_windy_image_is_fetched_fresh_and_kept_for_five_minutes(client, lists, http):
    body = _image(client, "windy:1000").json()
    assert body["url"] == "https://imgproxy.windy.com/p.jpg?token=abc"
    assert body["taken"] == "2026-10-16T17:08:00.000Z" and body["link"] == "https://x/1000"
    expires = datetime.fromisoformat(body["expires"])
    assert timedelta(minutes=9) < expires - datetime.now(UTC) <= timedelta(minutes=10)
    url, headers = http.calls[0]
    assert url == f"{cameras.WINDY_API}/webcams/1000" and headers["x-windy-api-key"] == WINDY_KEY
    _image(client, "windy:1000")
    assert len(http.calls) == 1


def test_windy_image_errors(client, lists, http, caplog):
    http.windy = (404, {"images": {}})
    assert _image(client, "windy:1000").status_code == 404
    http.windy = (500, {"images": {}})
    resp = _image(client, "windy:1000")
    assert resp.status_code == 502 and resp.headers["cache-control"] == "no-store"
    http.windy = (200, {"images": {"current": {}}})
    http_retry._BREAKERS.pop("windy", None)
    assert _image(client, "windy:1000").status_code == 502
    assert WINDY_KEY not in caplog.text


@pytest.mark.parametrize("camera_id", ["windy:999", "nps:NOPE", "cdot:1", "windy:../x", "x"])
def test_unknown_camera_is_404(client, lists, http, camera_id):
    assert _image(client, camera_id).status_code == 404
    assert http.calls == []


def test_windy_lookups_have_a_global_cap(client, lists, http, monkeypatch):
    monkeypatch.setitem(tile_tokens.BUCKETS, "windy_upstream", (parse("1/minute"),))
    lists["windy"]["cameras"].append(_cam("windy", "1001"))
    assert _image(client, "windy:1000").status_code == 200
    # A different IP and token, the same global counter.
    resp = client.get(f"/cameras/windy:1001/image?t={_token(tile_tokens.TIER_USER)}",
                      headers={"x-forwarded-for": "203.0.113.9"})
    assert resp.status_code == 429
    assert _image(client, "windy:1000").status_code == 200  # cached, no Windy call


# ── /status ───────────────────────────────────────────────────────────────────

def test_status_cameras(lists, monkeypatch):
    assert asyncio.run(status._check_cameras()).healthy is True
    lists["windy"] = _doc("windy", [], age=timedelta(days=4))
    health = asyncio.run(status._check_cameras())
    assert health.healthy is False and health.detail.startswith("colorado windy:")
    lists["windy"] = httpx.ConnectError("minio down")
    assert asyncio.run(status._check_cameras()).healthy is False
    monkeypatch.setattr(get_settings(), "windy_webcams_api_key", "")
    health = asyncio.run(status._check_cameras())
    assert health.healthy is True and health.detail == "windy: not configured"


def test_stored_list_round_trip(up, dem):
    """What the job writes is what the API reads."""
    store = FakeStore(NOW)
    _run(store, up)
    doc = json.loads(store.get(cameras.key("colorado", "nps")))
    assert cameras.find(doc, "nps:FRE")["image_url"].endswith("/fre.jpg")
    assert cameras.find(doc, "nps:missing") is None
