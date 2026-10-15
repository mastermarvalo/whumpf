"""/tiles/ski: the Martin proxy for ski runs, lifts and ski areas."""

from __future__ import annotations

import gzip
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.main import app
from app.routers import tiles

TILE = "/tiles/ski/14/3364/6233?region=colorado"
PBF = gzip.compress(b"\x1a\x08ski_runs")  # stand-in; the proxy never parses it


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch):
    # No backoff sleeps, and no breaker state leaking between tests.
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    monkeypatch.delitem(http_retry._BREAKERS, "martin", raising=False)
    yield
    http_retry._BREAKERS.pop("martin", None)


@pytest.fixture
def martin(monkeypatch) -> Iterator[dict]:
    """Stub Martin. Set state["response"] to (status, body, headers) or to an
    exception; every requested URL is appended to state["calls"]."""
    state: dict = {
        "calls": [],
        "response": (200, PBF, {"content-type": "application/x-protobuf", "content-encoding": "gzip"}),
    }

    async def fake_send(request: httpx.Request, stream: bool = False, **kwargs):
        state["calls"].append(str(request.url))
        state["accept_encoding"] = request.headers.get("accept-encoding")
        if isinstance(state["response"], Exception):
            raise state["response"]
        status, body, headers = state["response"]
        # An unread stream, like a real `send(..., stream=True)`.
        return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers, request=request)

    monkeypatch.setattr(tiles._HTTP, "send", fake_send)
    yield state


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _token() -> str:
    return tile_tokens.issue("i:install-0123456789abcdef", tile_tokens.TIER_ANON)[0]


def test_proxies_gzipped_pbf_from_the_region_source(client, martin):
    resp = client.get(f"{TILE}&t={_token()}")
    assert resp.status_code == 200
    assert martin["calls"] == [f"{get_settings().martin_url}/ski_colorado/14/3364/6233"]
    assert martin["accept_encoding"] == "gzip"
    assert resp.headers["content-type"] == "application/x-protobuf"
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["cache-control"] == "public, max-age=86400"
    # TestClient inflates gzip, so this checks the bytes went through untouched.
    assert resp.content == gzip.decompress(PBF)


def test_empty_tile_is_204(client, martin):
    martin["response"] = (204, b"", {})
    resp = client.get(TILE)
    assert resp.status_code == 204
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_missing_region_file_is_404_and_does_not_trip_the_breaker(client, martin):
    martin["response"] = (404, b"", {})
    for _ in range(6):
        assert client.get(TILE).status_code == 404
    assert http_retry.breaker("martin")._state() == "closed"


@pytest.mark.parametrize("region", ["utah", "../terrain_rgb", "colorado/../routes", ""])
def test_unknown_region_never_reaches_martin(client, martin, region):
    resp = client.get("/tiles/ski/14/3364/6233", params={"region": region})
    assert resp.status_code in (400, 422)
    assert martin["calls"] == []


@pytest.mark.parametrize("zxy", ["15/6728/12466", "-1/0/0", "14/16384/0", "14/0/16384", "3/-1/0"])
def test_out_of_range_tiles_are_400(client, martin, zxy):
    assert client.get(f"/tiles/ski/{zxy}?region=colorado").status_code == 400
    assert martin["calls"] == []


def test_martin_down_is_502_then_503_once_the_breaker_opens(client, martin):
    martin["response"] = httpx.ConnectError("connection refused")
    codes = [client.get(TILE).status_code for _ in range(6)]
    assert codes == [502] * 5 + [503]
    # 3 attempts each for the first five; the open breaker stops the sixth.
    assert len(martin["calls"]) == 15


def test_martin_restart_is_absorbed_by_retries(client, martin, monkeypatch):
    # The weekly publish restarts Martin: the first attempt is refused, the
    # retry gets the tile.
    real = tiles._HTTP.send
    attempts = {"n": 0}

    async def flaky_send(request, stream=False, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return await real(request, stream=stream, **kwargs)

    monkeypatch.setattr(tiles._HTTP, "send", flaky_send)
    assert client.get(TILE).status_code == 200


def test_martin_5xx_is_502(client, martin):
    martin["response"] = (500, b"boom", {})
    assert client.get(TILE).status_code == 502


# ── tile token and rate limit apply like every other /tiles endpoint ─────────

def test_missing_token_is_401_when_tokenless_is_off(client, martin, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    resp = client.get(TILE)
    assert resp.status_code == 401
    assert martin["calls"] == []
    assert client.get(f"{TILE}&t={_token()}").status_code == 200


@pytest.mark.parametrize("token", ["garbage", "a.b", ""])
def test_bad_token_is_401_before_martin(client, martin, token):
    assert client.get(TILE, params={"t": token}).status_code == 401
    assert martin["calls"] == []


def test_counts_against_the_anon_bucket(client, martin, monkeypatch):
    from limits import parse
    monkeypatch.setitem(tile_tokens.BUCKETS, "anon", (parse("2/minute"),))
    token = tile_tokens.issue("i:install-ski-ratelimit-0001", tile_tokens.TIER_ANON)[0]
    codes = [client.get(f"{TILE}&t={token}").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert len(martin["calls"]) == 2
