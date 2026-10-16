"""Fakes shared by the weather layer tests (snow depth, temperature, radar):
an in-memory MinIO store, a fake NOAA file server, a fake latest.json and a
fake TiTiler."""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from app import http_retry
from app.routers import tiles


class FakeStore:
    def __init__(self, now: datetime) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.puts: list[str] = []
        self.now = now

    def get(self, key):
        return self.objects[key][0] if key in self.objects else None

    def put(self, key, body, content_type):
        self.objects[key] = (body, self.now)
        self.puts.append(key)

    def list(self, prefix):
        return [(k, m) for k, (_, m) in self.objects.items() if k.startswith(prefix)]

    def delete(self, keys):
        for k in keys:
            self.objects.pop(k)

    def json(self, key: str) -> dict:
        return json.loads(self.get(key))


class FakeServer:
    """files[url] = (body, etag). Everything else is 404 (or `listing` for
    directory URLs). Matching If-None-Match gets 304; Range is honoured."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}
        self.requests: list[tuple[str, str | None]] = []
        self.down: set[str] = set()  # hosts that refuse connections

    def add(self, url: str, body: bytes, etag: str = '"e"') -> None:
        self.files[url] = (body, etag)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host in self.down:
            raise httpx.ConnectError("down", request=request)
        inm = request.headers.get("if-none-match")
        self.requests.append((url, inm))
        if url not in self.files:
            return httpx.Response(404)
        body, etag = self.files[url]
        if inm == etag:
            return httpx.Response(304, headers={"etag": etag})
        if rng := request.headers.get("range"):
            lo, hi = (int(v) for v in rng.removeprefix("bytes=").split("-"))
            return httpx.Response(206, content=body[lo:hi + 1], headers={"etag": etag})
        return httpx.Response(200, content=body, headers={"etag": etag})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def reset_breakers(monkeypatch, *names: str) -> None:
    for name in names:
        monkeypatch.delitem(http_retry._BREAKERS, name, raising=False)


def fake_pointer(monkeypatch, module, value) -> dict:
    """Make `module.read_pointer` return state["value"] (raise it if it is
    an exception). Tests edit the state in place."""
    state = {"value": value}

    async def fake_read(region):
        if isinstance(state["value"], Exception):
            raise state["value"]
        return state["value"]

    monkeypatch.setattr(module, "read_pointer", fake_read)
    return state


def fake_titiler(monkeypatch) -> dict:
    """Record TiTiler calls and answer state["status"] with a fake PNG."""
    state: dict = {"status": 200, "calls": []}

    async def fake_get(url, params=None, **kwargs):
        state["calls"].append((url, params))
        return httpx.Response(
            state["status"], content=b"\x89PNG fake", headers={"content-type": "image/png"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)
    return state
