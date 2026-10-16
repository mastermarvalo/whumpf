"""Fakes shared by the weather layer tests (snow depth, temperature, wind,
radar): an in-memory MinIO store, a fake NOAA file server, RTMA messages, a
fake latest.json and a fake TiTiler."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

import httpx
import numpy as np
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

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
        self.ranges: list[str | None] = []
        self.down: set[str] = set()  # hosts that refuse connections

    def add(self, url: str, body: bytes, etag: str = '"e"') -> None:
        self.files[url] = (body, etag)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host in self.down:
            raise httpx.ConnectError("down", request=request)
        inm = request.headers.get("if-none-match")
        self.requests.append((url, inm))
        self.ranges.append(request.headers.get("range"))
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


# ── RTMA ──────────────────────────────────────────────────────────────────────
# A GeoTIFF on the NDFD Lambert grid with the band tags GDAL reports for an
# RTMA GRIB2 message (the jobs only look at those, not the driver).

LCC = (
    "+proj=lcc +lat_0=25 +lon_0=-95 +lat_1=25 +lat_2=25 +x_0=0 +y_0=0 "
    "+a=6371200 +b=6371200 +units=m +no_defs"
)
RES_M = 2539.703


def rtma_message(
    hour: datetime, *, value: float = 5.0, corner: float | None = -10.0,
    element: str = "TMP", unit: str = "[C]", crs: str = LCC, res: float = RES_M,
    fill: Callable[[np.ndarray], None] | None = None, nodata: float | None = None,
) -> bytes:
    """`value` everywhere over Colorado and around it, with `corner` in the
    north-west quarter (unless None). `fill` may then edit the grid in place
    (rows north to south)."""
    to_lcc = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = to_lcc.transform([-111, -111, -99, -99], [35, 43, 35, 43])
    x0, y1 = min(xs), max(ys)
    w, h = int((max(xs) - x0) / res) + 1, int((y1 - min(ys)) / res) + 1
    data = np.full((h, w), value, dtype=np.float64)
    if corner is not None:
        data[: h // 4, : w // 4] = corner
    if fill:
        fill(data)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff", width=w, height=h, count=1, dtype="float64", crs=crs,
            transform=from_origin(x0, y1, res, res), nodata=nodata,
        ) as dst:
            dst.write(data, 1)
            dst.update_tags(1, GRIB_ELEMENT=element, GRIB_UNIT=unit,
                            GRIB_VALID_TIME=str(int(hour.timestamp())))
        return mem.read()


def wind_messages(
    hour: datetime, *, direction: float = 270.0, speed: float = 10.0, gust: float = 15.0,
) -> dict[str, bytes]:
    """WDIR (degrees), WIND and GUST (m/s), the same everywhere."""
    return {
        "WDIR": rtma_message(hour, value=direction, corner=None, element="WDIR", unit="[deg true]"),
        "WIND": rtma_message(hour, value=speed, corner=None, element="WIND", unit="[m/s]"),
        "GUST": rtma_message(hour, value=gust, corner=None, element="GUST", unit="[m/s]"),
    }


def add_rtma_hour(
    server: FakeServer, url: str, hour: datetime, messages: dict[str, bytes],
) -> None:
    """The full analysis file at `url` with its .idx, in RTMA's order:
    HGT, PRES, TMP, DPT, WDIR, WIND, GUST, VIS. `messages` holds TMP and
    the wind fields; a missing one is left out of the file and the index."""
    levels = {"TMP": "2 m above ground", "WDIR": "10 m above ground",
              "WIND": "10 m above ground", "GUST": "10 m above ground"}
    parts = [("HGT", "surface", b"H" * 100), ("PRES", "surface", b"P" * 50)]
    for var in ("TMP", "DPT", "WDIR", "WIND", "GUST"):
        if var == "DPT":
            parts.append(("DPT", "2 m above ground", b"D" * 70))
        elif var in messages:
            parts.append((var, levels[var], messages[var]))
    parts.append(("VIS", "surface", b"V" * 30))
    d = f"d={hour:%Y%m%d%H}"
    body, lines = b"", []
    for n, (var, level, data) in enumerate(parts, 1):
        lines.append(f"{n}:{len(body)}:{d}:{var}:{level}:anl:")
        body += data
    server.add(url, body)
    server.add(f"{url}.idx", ("\n".join(lines) + "\n").encode())


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
