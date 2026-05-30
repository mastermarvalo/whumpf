"""Regression guard for slope tile sizing.

The slope layer is served as 512×512 tiles for 2:1 downscale anti-aliasing, and
must NOT use TiTiler's `buffer` param: `buffer` returns tilesize+2*buffer px of
neighbouring data, which a MapLibre raster source redraws inside the tile's own
extent — duplicating a strip of the neighbour's slope at every tile seam.
"""

from __future__ import annotations

import struct
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from PIL import Image

import app.routers.tiles as tiles
from app.main import app


def _png_bytes(w: int, h: int) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (w, h)).save(buf, "PNG")
    return buf.getvalue()


def test_slope_tile_is_512_and_unbuffered(monkeypatch):
    captured: dict = {}

    async def fake_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params or {}
        # raise_for_status() in the endpoint needs a bound request.
        return httpx.Response(
            200, content=_png_bytes(512, 512),
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", url, params=params),
        )

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)

    client = TestClient(app)
    resp = client.get("/tiles/slope/10/210/394?region=colorado")
    assert resp.status_code == 200

    # The invariants that prevent duplicated slope at tile borders:
    assert str(captured["params"].get("tilesize")) == "512"
    assert "buffer" not in captured["params"], "buffer bakes neighbour pixels into the tile"

    # And the served PNG is genuinely 512×512 (not tilesize + 2*buffer).
    width, height = struct.unpack(">II", resp.content[16:24])
    assert (width, height) == (512, 512)
