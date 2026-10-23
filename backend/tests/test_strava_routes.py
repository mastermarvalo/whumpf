"""Strava API terms: Strava data may only be shown to the athlete it belongs to.

Strava-sourced routes (``source_strava_id`` set) can't be shared, can only be
cloned by their owner (and the clone keeps its origin), can't be labelled
shared/unlisted, and are hidden from other trip members.

Routes are created via POST /routes and then marked as Strava-sourced directly,
so no Strava API mocking is needed. Terrain sampling + CAIC lookup are mocked.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.route import Route, Visibility
from app.models.route_share import RouteShare
from app.services.cog_sampler import TerrainSample

_LINE = {
    "type": "LineString",
    "coordinates": [[-106.40, 39.10], [-106.39, 39.11], [-106.38, 39.115]],
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
        return {"zone": "Aspen Zone", "danger": {}, "problems": []}

    async def _no_email(*args, **kwargs):
        return None

    monkeypatch.setattr("app.routers.routes.sample_polyline", _fake_sample)
    monkeypatch.setattr("app.routers.trips.get_zone_detail_for_point", _fake_zone)
    monkeypatch.setattr("app.routers.trips.send_trip_invite", _no_email)


def _make_route(client, name="R"):
    resp = client.post("/routes", json={"name": name, "region": "colorado", "geometry": _LINE})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_strava_route(client, db_session, name="Strava R", activity_id=12345):
    rid = _make_route(client, name)
    db_session.get(Route, rid).source_strava_id = activity_id
    db_session.flush()
    return rid


def _legacy_share(db_session, rid, user):
    """A share link created before Strava routes were refused sharing."""
    share = RouteShare(route_id=rid, token=f"legacy-{rid}", created_by_id=user.id)
    db_session.add(share)
    db_session.flush()
    return share.token


# --------------------------------------------------------------------------- #
# Share
# --------------------------------------------------------------------------- #
def test_share_strava_route_is_refused(make_client, db_session):
    rid = _make_strava_route(make_client("owner"), db_session)

    resp = make_client("owner").post(f"/routes/{rid}/share")
    assert resp.status_code == 409
    assert "Strava" in resp.json()["detail"]

    shares = db_session.scalars(select(RouteShare).where(RouteShare.route_id == rid)).all()
    assert shares == []
    assert db_session.get(Route, rid).visibility == Visibility.private


def test_share_strava_route_by_non_owner_is_still_403(make_client, db_session):
    rid = _make_strava_route(make_client("owner"), db_session)
    assert make_client("other").post(f"/routes/{rid}/share").status_code == 403


def test_legacy_share_token_no_longer_grants_view(make_client, db_session, users):
    rid = _make_strava_route(make_client("owner"), db_session)
    token = _legacy_share(db_session, rid, users["owner"])

    assert make_client("other").get(f"/routes/{rid}", params={"token": token}).status_code == 404
    assert make_client("owner").get(f"/routes/{rid}").status_code == 200


# --------------------------------------------------------------------------- #
# Clone
# --------------------------------------------------------------------------- #
def test_owner_can_clone_own_strava_route_and_clone_keeps_origin(make_client, db_session):
    rid = _make_strava_route(make_client("owner"), db_session, activity_id=777)

    resp = make_client("owner").post(f"/routes/{rid}/clone")
    assert resp.status_code == 201, resp.text
    clone = db_session.get(Route, resp.json()["id"])
    assert clone.id != rid
    assert clone.source_strava_id == 777
    assert clone.visibility == Visibility.private

    # The clone is Strava-sourced too, so it can't be shared either.
    assert make_client("owner").post(f"/routes/{clone.id}/share").status_code == 409


def test_non_owner_with_legacy_token_cannot_clone_strava_route(make_client, db_session, users):
    rid = _make_strava_route(make_client("owner"), db_session)
    token = _legacy_share(db_session, rid, users["owner"])

    resp = make_client("other").post(f"/routes/{rid}/clone", params={"token": token})
    assert resp.status_code == 409
    assert "Strava" in resp.json()["detail"]
    assert db_session.scalars(select(Route).where(Route.owner_id == users["other"].id)).all() == []


def test_non_owner_without_token_cannot_clone_strava_route(make_client, db_session):
    rid = _make_strava_route(make_client("owner"), db_session)
    # No token → same 404 as any route they can't see (doesn't reveal Strava origin).
    assert make_client("other").post(f"/routes/{rid}/clone").status_code == 404
    assert make_client("other").post(
        f"/routes/{rid}/clone", params={"token": "bogus"}
    ).status_code == 404


def test_plain_route_clone_still_works_via_token(make_client):
    rid = _make_route(make_client("owner"))
    token = make_client("owner").post(f"/routes/{rid}/share").json()["token"]
    resp = make_client("other").post(f"/routes/{rid}/clone", params={"token": token})
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Visibility label
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("visibility", ["shared", "unlisted"])
def test_cannot_label_strava_route_shared(make_client, db_session, visibility):
    rid = _make_strava_route(make_client("owner"), db_session)
    resp = make_client("owner").patch(f"/routes/{rid}", json={"visibility": visibility})
    assert resp.status_code == 409
    assert db_session.get(Route, rid).visibility == Visibility.private


def test_strava_route_other_edits_and_private_label_allowed(make_client, db_session):
    rid = _make_strava_route(make_client("owner"), db_session)
    resp = make_client("owner").patch(
        f"/routes/{rid}", json={"name": "Renamed", "visibility": "private"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


def test_plain_route_can_still_be_labelled_shared(make_client):
    rid = _make_route(make_client("owner"))
    resp = make_client("owner").patch(f"/routes/{rid}", json={"visibility": "shared"})
    assert resp.status_code == 200 and resp.json()["visibility"] == "shared"


# --------------------------------------------------------------------------- #
# Trips
# --------------------------------------------------------------------------- #
def _route_ids(trip_body):
    return {r["id"] for day in trip_body["days"] for r in day["routes"]}


def test_trip_hides_strava_routes_from_other_members(make_client, db_session):
    owner_plain = _make_route(make_client("owner"), "Owner plain")
    owner_strava = _make_strava_route(make_client("owner"), db_session, "Owner strava", 1)

    resp = make_client("owner").post("/trips", json={
        "name": "Trip", "date": "2026-06-01", "region": "colorado",
        "days": [{"route_ids": [owner_plain, owner_strava]}],
    })
    assert resp.status_code == 201, resp.text
    assert _route_ids(resp.json()) == {owner_plain, owner_strava}
    tid = resp.json()["id"]

    make_client("owner").post(f"/trips/{tid}/members", json={"email": "other@example.com"})
    assert make_client("other").post(
        f"/trips/{tid}/members/respond", json={"action": "accept"}
    ).status_code == 204

    # Member adds their own plain + Strava routes; the add response is theirs.
    other_plain = _make_route(make_client("other"), "Other plain")
    other_strava = _make_strava_route(make_client("other"), db_session, "Other strava", 2)
    make_client("other").post(f"/trips/{tid}/routes", json={"route_id": other_plain})
    resp = make_client("other").post(f"/trips/{tid}/routes", json={"route_id": other_strava})
    assert resp.status_code == 201, resp.text
    assert _route_ids(resp.json()) == {owner_plain, other_plain, other_strava}

    owner_view = make_client("owner").get(f"/trips/{tid}").json()
    assert _route_ids(owner_view) == {owner_plain, owner_strava, other_plain}

    other_view = make_client("other").get(f"/trips/{tid}").json()
    assert _route_ids(other_view) == {owner_plain, other_plain, other_strava}

    # PATCH response is filtered for the caller too.
    patched = make_client("owner").patch(f"/trips/{tid}", json={"notes": "x"}).json()
    assert _route_ids(patched) == {owner_plain, owner_strava, other_plain}


def test_forecast_snapshot_skips_strava_routes(make_client, db_session, monkeypatch):
    calls = []

    async def _record_zone(lat, lng):
        calls.append((lat, lng))
        return {"zone": "Aspen Zone", "danger": {}, "problems": []}

    monkeypatch.setattr("app.routers.trips.get_zone_detail_for_point", _record_zone)

    strava = _make_strava_route(make_client("owner"), db_session)
    plain_line = {"type": "LineString", "coordinates": [[-105.80, 39.60], [-105.79, 39.61]]}
    plain = make_client("owner").post(
        "/routes", json={"name": "Plain", "region": "colorado", "geometry": plain_line}
    ).json()["id"]

    resp = make_client("owner").post("/trips", json={
        "name": "Trip", "date": "2026-06-01", "region": "colorado",
        "days": [{"route_ids": [strava, plain]}],
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["caic_zone"] == "Aspen Zone"
    assert calls == [(pytest.approx(39.60), pytest.approx(-105.80))]  # plain route's start


def test_strava_only_trip_has_no_forecast_snapshot(make_client, db_session):
    strava = _make_strava_route(make_client("owner"), db_session)
    resp = make_client("owner").post("/trips", json={
        "name": "Trip", "date": "2026-06-01", "region": "colorado",
        "days": [{"route_ids": [strava]}],
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["caic_zone"] is None
    assert resp.json()["forecast_snapshot"] is None


# --------------------------------------------------------------------------- #
# 7-day rule bookkeeping (app/jobs/strava_refresh.py)
# --------------------------------------------------------------------------- #
def test_import_records_when_strava_data_was_fetched(make_client, db_session, users, monkeypatch):
    from datetime import UTC, datetime

    from tests import strava_fakes

    fake = strava_fakes.install(monkeypatch)
    fake.activities = {555: {"id": 555, "name": "Dawn patrol",
                             "map": {"polyline": strava_fakes.encode_polyline(strava_fakes.TRACK)}}}
    strava_fakes.connect(db_session, users["owner"], 4242)

    before = datetime.now(UTC)
    resp = make_client("owner").post("/routes/import/strava/555", params={"region": "colorado"})
    assert resp.status_code == 201, resp.text
    route = db_session.get(Route, resp.json()["id"])
    assert route.source_strava_id == 555
    assert route.strava_fetched_at >= before


def test_clone_keeps_the_fetch_time_of_the_original(make_client, db_session):
    from datetime import UTC, datetime, timedelta

    rid = _make_strava_route(make_client("owner"), db_session, activity_id=778)
    fetched = datetime.now(UTC) - timedelta(days=5)
    db_session.get(Route, rid).strava_fetched_at = fetched
    db_session.flush()

    resp = make_client("owner").post(f"/routes/{rid}/clone")
    assert resp.status_code == 201, resp.text
    assert db_session.get(Route, resp.json()["id"]).strava_fetched_at == fetched


def test_drawn_route_has_no_fetch_time(make_client, db_session):
    rid = _make_route(make_client("owner"))
    assert db_session.get(Route, rid).strava_fetched_at is None
