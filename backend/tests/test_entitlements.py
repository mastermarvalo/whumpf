"""Layer access by tier (app/entitlements.py): open to every tier today,
and making a layer pro-only is a change to ALLOWED_TIERS alone, which pack
requests, the manifest and tile routes all follow. Also the shared RGB COG
writer and tile formats that the Sentinel layer builds on."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

import numpy as np
import pytest
import rasterio
from fastapi import APIRouter, Depends, Request
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from app import entitlements, tile_tokens
from app.main import app
from app.routers import tiles
from app.services import rasterlayer
from app.tile_tokens import TIER_ANON, TIER_USER, require_tile_access
from tests.test_packs_api import ALL, BERTHOUD, MANIFEST_LAYERS, client, r2, versions  # noqa: F401
from tests.weather_fakes import fake_titiler


def test_every_tier_may_use_every_layer_today():
    for tier in (TIER_ANON, TIER_USER):
        for layer in ("sentinel", "naip", "topo", "cameras"):
            assert entitlements.allowed(tier, layer) and entitlements.access(tier, layer) == "ok"
        entitlements.require(tier, ["sentinel", "naip"])


def test_cameras_are_never_gated():
    assert "cameras" not in entitlements.ALLOWED_TIERS


@pytest.fixture
def pro_naip(monkeypatch):
    """naip for a "pro" tier only, as a paid tier would be set up."""
    monkeypatch.setitem(entitlements.ALLOWED_TIERS, "naip", frozenset({"pro"}))


def test_a_gated_layer_is_refused_with_a_code(pro_naip):
    assert entitlements.access(TIER_USER, "naip") == "pro_required"
    with pytest.raises(entitlements.NotEntitled) as exc:
        entitlements.require(TIER_USER, ["topo", "naip"])
    assert exc.value.status_code == 403 and exc.value.layer == "naip"


def test_pack_requests_follow_the_table(client, pro_naip):  # noqa: F811
    c = client("owner")
    r = c.post("/packs", json={"region": "colorado", "bbox": BERTHOUD, "layers": ALL + ["naip"]})
    assert r.status_code == 403 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"detail": entitlements.MESSAGE, "code": "pro_required", "layer": "naip"}
    # Shipped builds' requests don't touch the gated layer.
    assert c.post("/packs", json={"region": "colorado", "bbox": BERTHOUD, "layers": ALL}).status_code == 202

    layers = {layer["id"]: layer for layer in c.get("/packs/manifest?region=colorado").json()["layers"]}
    assert layers["naip"]["access"] == "pro_required" and layers["topo"]["access"] == "ok"


def test_manifest_access_is_ok_everywhere_today(client):  # noqa: F811
    layers = client("owner").get("/packs/manifest?region=colorado").json()["layers"]
    assert [layer["id"] for layer in layers] == MANIFEST_LAYERS
    assert {layer["access"] for layer in layers} == {"ok"}


# A tile route as a gated layer would write it.
_probe = APIRouter(prefix="/tiles", dependencies=[Depends(require_tile_access)])


@_probe.get("/_gated_probe")
def _gated_probe(request: Request) -> dict:
    entitlements.require(entitlements.tile_tier(request), "naip")
    return {"tier": entitlements.tile_tier(request)}


@pytest.fixture
def probe_client():
    app.include_router(_probe)
    yield TestClient(app)
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/tiles/_gated_probe"]


def test_tile_routes_follow_the_token_tier(probe_client, monkeypatch):
    anon = tile_tokens.issue("i:install-0123456789abcdef", TIER_ANON)[0]
    user = tile_tokens.issue("u:42", TIER_USER)[0]
    assert probe_client.get(f"/tiles/_gated_probe?t={anon}").json() == {"tier": "anon"}
    assert probe_client.get(f"/tiles/_gated_probe?t={user}").json() == {"tier": "user"}
    assert probe_client.get("/tiles/_gated_probe").json() == {"tier": "anon"}  # tokenless (old site)

    monkeypatch.setitem(entitlements.ALLOWED_TIERS, "naip", frozenset({TIER_USER}))
    r = probe_client.get(f"/tiles/_gated_probe?t={anon}")
    assert r.status_code == 403 and r.json()["code"] == "pro_required"
    assert probe_client.get(f"/tiles/_gated_probe?t={user}").status_code == 200


# ── shared raster pieces ─────────────────────────────────────────────────────

def test_rgb_cog_keeps_the_mask_and_has_overviews(tmp_path):
    src, dst = tmp_path / "src.tif", tmp_path / "out.tif"
    rgb = np.zeros((3, 2048, 2048), dtype=np.uint8)
    rgb[0] = 200
    mask = np.zeros((2048, 2048), dtype=np.uint8)
    mask[:, :1024] = 255  # the east half has no data
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK="YES"), rasterio.open(
            src, "w", driver="GTiff", width=2048, height=2048, count=3, dtype="uint8", tiled=True,
            crs="EPSG:3857", transform=from_origin(-11_800_000, 4_850_000, 10, 10)) as out:
        out.write(rgb)
        out.write_mask(mask)
    rasterlayer.to_cog(str(src), str(dst), compress="JPEG", quality=85)
    with rasterio.open(dst) as cog:
        assert cog.profile["compress"].lower() == "jpeg" and cog.overviews(1)
        assert cog.block_shapes[0] == (512, 512)
        got = cog.dataset_mask()
        assert got[:, :1000].min() == 255 and got[:, 1100:].max() == 0


@pytest.mark.parametrize("fmt, media_type", [("png", "image/png"), ("jpg", "image/jpeg"),
                                             ("webp", "image/webp")])
def test_cog_tile_formats(monkeypatch, fmt, media_type):
    titiler = fake_titiler(monkeypatch)
    response = asyncio.run(tiles.cog_tile(OrderedDict(), 4, ("k", fmt), "x/y.tif", {}, {}, "probe", 10, 1, 2,
                                          fmt=fmt))
    assert response.media_type == media_type
    assert titiler["calls"][0][0].endswith(f"/cog/tiles/WebMercatorQuad/10/1/2.{fmt}")
