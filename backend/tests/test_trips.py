"""Phase C: trips — creation/forecast snapshot, membership access, invites,
respond, and waypoint CRUD. Terrain sampling + CAIC zone lookup are mocked.
"""

from __future__ import annotations

import pytest

from app.services.cog_sampler import TerrainSample

_LINE = {
    "type": "LineString",
    "coordinates": [[-106.40, 39.10], [-106.39, 39.11], [-106.38, 39.115]],
}

_SNAPSHOT = {
    "zone": "Aspen Zone",
    "forecaster": "CAIC",
    "valid_date": "2026-05-31",
    "danger": {"alp": "considerable", "tln": "moderate", "btl": "low"},
    "problems": [],
    "link": "https://avalanche.state.co.us/",
}


@pytest.fixture(autouse=True)
def _mocks(monkeypatch):
    def _fake_sample(vertices, region, settings, n_total=128):
        return [
            TerrainSample(
                distance_m=(i / (n_total - 1)) * 1500.0,
                elevation_m=3000.0,
                slope_deg=20.0,
                aspect_deg=45.0,
            )
            for i in range(n_total)
        ]

    async def _fake_zone(lat, lng):
        return dict(_SNAPSHOT)

    monkeypatch.setattr("app.routers.routes.sample_polyline", _fake_sample)
    monkeypatch.setattr("app.routers.trips.get_zone_detail_for_point", _fake_zone)


def _make_route(client, name="R"):
    return client.post(
        "/routes", json={"name": name, "region": "colorado", "geometry": _LINE}
    ).json()["id"]


def _create_trip(client, route_ids, name="Trip", date="2026-06-01", days=None):
    payload = {
        "name": name, "date": date, "region": "colorado",
        "days": days if days is not None else [{"route_ids": route_ids}],
    }
    return client.post("/trips", json=payload)


def test_create_trip_freezes_forecast_and_auto_adds_owner(make_client, users):
    owner = make_client("owner")
    rid = _make_route(owner)
    resp = _create_trip(owner, [rid])
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["caic_zone"] == "Aspen Zone"
    assert body["forecast_snapshot"]["danger"]["alp"] == "considerable"
    assert body["num_days"] == 1
    assert len(body["days"]) == 1
    assert len(body["days"][0]["routes"]) == 1 and len(body["days"][0]["routes"][0]["samples"]) > 0
    owners = [m for m in body["members"] if m["role"] == "owner"]
    assert len(owners) == 1
    assert owners[0]["status"] == "accepted"
    assert owners[0]["user_id"] == users["owner"].id


def test_create_trip_without_routes_has_no_snapshot(make_client):
    resp = _create_trip(make_client("owner"), [])
    assert resp.status_code == 201
    body = resp.json()
    assert body["caic_zone"] is None
    assert body["forecast_snapshot"] is None
    assert len(body["days"]) == 1 and body["days"][0]["routes"] == []


def test_multi_day_trip_assigns_routes_to_days(make_client):
    owner = make_client("owner")
    r1 = _make_route(owner, "Day one")
    r2 = _make_route(owner, "Day two")
    resp = _create_trip(owner, [], days=[{"route_ids": [r1]}, {"route_ids": [r2]}])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["num_days"] == 2
    assert len(body["days"]) == 2
    assert body["days"][0]["routes"][0]["name"] == "Day one"
    assert body["days"][1]["routes"][0]["name"] == "Day two"
    assert body["days"][0]["date"] == "2026-06-01"
    assert body["days"][1]["date"] == "2026-06-02"  # start + 1


def test_cannot_attach_unowned_route(make_client):
    rid = _make_route(make_client("owner"))
    # `other` doesn't own that route.
    assert _create_trip(make_client("other"), [rid]).status_code == 400


def test_list_and_view_are_member_only(make_client):
    owner = make_client("owner")
    tid = _create_trip(owner, [_make_route(owner)]).json()["id"]
    assert len(owner.get("/trips").json()) == 1

    other = make_client("other")
    assert other.get("/trips").json() == []
    assert other.get(f"/trips/{tid}").status_code == 404  # non-member, don't leak


def test_owner_only_edit_delete(make_client):
    owner = make_client("owner")
    tid = _create_trip(owner, [_make_route(owner)]).json()["id"]
    other = make_client("other")
    assert other.patch(f"/trips/{tid}", json={"name": "hijack"}).status_code == 403
    assert other.delete(f"/trips/{tid}").status_code == 403


def test_invite_respond_flow(make_client):
    owner = make_client("owner")
    tid = _create_trip(owner, [_make_route(owner)]).json()["id"]

    inv = owner.post(f"/trips/{tid}/members", json={"email": "other@example.com"})
    assert inv.status_code == 201 and inv.json()["status"] == "invited"

    # Idempotent.
    assert owner.post(f"/trips/{tid}/members", json={"email": "other@example.com"}).status_code == 200

    other = make_client("other")
    assert len(other.get("/trips/invites").json()) == 1
    assert other.get(f"/trips/{tid}").status_code == 404  # not accepted yet
    assert other.post(f"/trips/{tid}/members/respond", json={"action": "accept"}).status_code == 204

    other = make_client("other")
    assert any(t["id"] == tid for t in other.get("/trips").json())
    assert other.get(f"/trips/{tid}").status_code == 200


def test_invite_requires_owner(make_client):
    owner = make_client("owner")
    tid = _create_trip(owner, [_make_route(owner)]).json()["id"]
    assert make_client("other").post(
        f"/trips/{tid}/members", json={"email": "third@example.com"}
    ).status_code == 403


def test_invite_any_email_non_user(make_client):
    owner = make_client("owner")
    tid = _create_trip(owner, [_make_route(owner)]).json()["id"]
    resp = owner.post(f"/trips/{tid}/members", json={"email": "stranger@example.com"})
    assert resp.status_code == 201
    assert resp.json()["user_id"] is None


def test_waypoint_crud_by_members(make_client):
    owner = make_client("owner")
    tid = _create_trip(owner, [_make_route(owner)]).json()["id"]

    # Non-member can't add.
    assert make_client("other").post(
        f"/trips/{tid}/waypoints", json={"lng": -106.4, "lat": 39.1, "kind": "parking"}
    ).status_code == 404

    owner = make_client("owner")
    wp = owner.post(
        f"/trips/{tid}/waypoints",
        json={"lng": -106.4, "lat": 39.1, "elevation_m": 2800, "kind": "trailhead", "label": "TH"},
    )
    assert wp.status_code == 201, wp.text
    body = wp.json()
    assert body["geometry"]["type"] == "Point"
    assert len(body["geometry"]["coordinates"]) == 3
    assert body["kind"] == "trailhead"
    wid = body["id"]

    # Shows in trip detail.
    assert len(owner.get(f"/trips/{tid}").json()["waypoints"]) == 1

    # Update + delete.
    upd = owner.patch(f"/trips/{tid}/waypoints/{wid}", json={"label": "Parking lot", "kind": "parking"})
    assert upd.status_code == 200 and upd.json()["label"] == "Parking lot"
    assert owner.delete(f"/trips/{tid}/waypoints/{wid}").status_code == 204
    assert owner.get(f"/trips/{tid}").json()["waypoints"] == []
