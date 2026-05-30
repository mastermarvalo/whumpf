"""API tests for the saved-routes endpoints — ownership/visibility focus.

Terrain sampling is mocked so these run without COG/network access.
"""

from __future__ import annotations

import pytest

from app.models.strava import StravaConnection
from app.services.cog_sampler import TerrainSample

# A short A→B line in Colorado.
_LINE = {
    "type": "LineString",
    "coordinates": [[-106.40, 39.10], [-106.39, 39.11], [-106.38, 39.115]],
}


@pytest.fixture(autouse=True)
def _mock_sampling(monkeypatch):
    """Replace the COG read with deterministic synthetic samples."""

    def _fake(vertices, region, settings, n_total=128):
        out = []
        for i in range(n_total):
            frac = i / (n_total - 1)
            out.append(
                TerrainSample(
                    distance_m=frac * 1500.0,
                    elevation_m=3000.0 + frac * 200.0,
                    slope_deg=10.0 + frac * 25.0,
                    aspect_deg=(frac * 360.0) % 360.0,
                )
            )
        return out

    monkeypatch.setattr("app.routers.routes.sample_polyline", _fake)


def _create(client, name="Test route", region="colorado", geometry=None):
    body = {"name": name, "region": region, "geometry": geometry or _LINE}
    return client.post("/routes", json=body)


def test_create_route_roundtrips(make_client):
    client = make_client("owner")
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["name"] == "Test route"
    assert body["region"] == "colorado"
    assert body["visibility"] == "private"  # private by default
    assert body["owner_id"] is not None

    # Geometry round-trips as a LineString with Z populated per vertex.
    geom = body["geometry"]
    assert geom["type"] == "LineString"
    assert len(geom["coordinates"]) == 3
    assert all(len(c) == 3 for c in geom["coordinates"])  # x, y, z

    # Summary + cached samples are stored.
    assert body["summary"]["distance_m"] > 0
    assert "slope_distribution" in body["summary"]
    assert len(body["samples"]) > 0
    assert body["samples"][0]["aspect"] is not None


def test_get_route_as_owner(make_client):
    client = make_client("owner")
    rid = _create(client).json()["id"]
    resp = client.get(f"/routes/{rid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == rid


def test_list_returns_only_own_routes(make_client):
    owner = make_client("owner")
    _create(owner)

    assert len(owner.get("/routes").json()) == 1

    other = make_client("other")
    assert other.get("/routes").json() == []


def test_non_owner_get_is_404(make_client):
    rid = _create(make_client("owner")).json()["id"]
    resp = make_client("other").get(f"/routes/{rid}")
    assert resp.status_code == 404


def test_non_owner_patch_is_403(make_client):
    rid = _create(make_client("owner")).json()["id"]
    resp = make_client("other").patch(f"/routes/{rid}", json={"name": "hijack"})
    assert resp.status_code == 403


def test_non_owner_delete_is_403(make_client):
    rid = _create(make_client("owner")).json()["id"]
    resp = make_client("other").delete(f"/routes/{rid}")
    assert resp.status_code == 403


def test_owner_can_patch_and_delete(make_client):
    client = make_client("owner")
    rid = _create(client).json()["id"]

    patched = client.patch(
        f"/routes/{rid}", json={"name": "Renamed", "visibility": "unlisted"}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["visibility"] == "unlisted"

    assert client.delete(f"/routes/{rid}").status_code == 204
    assert client.get(f"/routes/{rid}").status_code == 404


def test_unknown_region_rejected(make_client):
    resp = _create(make_client("owner"), region="narnia")
    assert resp.status_code == 400


def test_too_few_vertices_rejected(make_client):
    geom = {"type": "LineString", "coordinates": [[-106.4, 39.1]]}
    resp = _create(make_client("owner"), geometry=geom)
    assert resp.status_code == 400


def test_over_distance_rejected(make_client):
    geom = {"type": "LineString", "coordinates": [[-109.0, 37.0], [-102.0, 41.0]]}
    resp = _create(make_client("owner"), geometry=geom)
    assert resp.status_code == 400


def test_import_strava_requires_connection(make_client):
    resp = make_client("owner").post("/routes/import/strava/123?region=colorado")
    assert resp.status_code == 404


def test_import_strava_creates_route(make_client, users, db_session, monkeypatch):
    db_session.add(StravaConnection(
        user_id=users["owner"].id, strava_athlete_id=999,
        access_token="a", refresh_token="r", expires_at=0,
    ))
    db_session.flush()

    async def fake_refresh(conn, session):
        return "tok"

    async def fake_detail(token, activity_id):
        return {"name": "Ski tour", "map": {"polyline": "encoded"}}

    monkeypatch.setattr("app.routers.routes.strava_svc.refresh_token", fake_refresh)
    monkeypatch.setattr("app.routers.routes.strava_svc.fetch_activity_detail", fake_detail)
    monkeypatch.setattr(
        "app.routers.routes.strava_svc._decode_polyline",
        lambda enc: [[-106.40, 39.10], [-106.39, 39.11], [-106.38, 39.115]],
    )

    client = make_client("owner")
    resp = client.post("/routes/import/strava/123?region=colorado")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Ski tour"
    assert body["visibility"] == "private"
    assert body["notes"] == "Imported from Strava"
    assert len(body["samples"]) > 0

    # Re-importing the same activity is idempotent: returns the existing route
    # (200), creates no duplicate.
    resp2 = client.post("/routes/import/strava/123?region=colorado")
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["id"] == body["id"]
    assert len(client.get("/routes").json()) == 1
