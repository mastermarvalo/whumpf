"""Strava webhook (GET validation, POST events) and disconnect deleting
Strava data. Strava's API Policy §2.5, §6.2-6.3, §7.4; docs/runbook.md §14.

Events aren't signed, so nothing is deleted on an event alone: these tests
check that Strava is asked first and that only what Strava confirms goes.
Strava's HTTP API is faked (tests/strava_fakes.py).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from limits import parse
from sqlalchemy import select

from app import log_redact, tile_tokens
from app.config import get_settings
from app.main import app
from app.models.route import Route
from app.models.route_share import RouteShare
from app.models.strava import StravaConnection
from app.models.trip import Trip
from app.models.trip_route import TripRoute
from app.services import strava_data
from tests import strava_fakes
from tests.strava_fakes import connect, make_route

VERIFY = "verify-token-0123456789"
ATHLETE = 4242
OTHER_ATHLETE = 5151


@pytest.fixture
def strava(monkeypatch):
    return strava_fakes.install(monkeypatch)


@pytest.fixture(autouse=True)
def webhook_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "strava_webhook_verify_token", VERIFY)


@pytest.fixture(autouse=True)
def job_db(monkeypatch, db_session):
    """The webhook's background task opens its own session; give it the
    test's."""
    @contextmanager
    def _shared():
        yield db_session

    monkeypatch.setattr("app.routers.strava.job_session", _shared)


@pytest.fixture(autouse=True)
def emails(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("app.services.email.send_strava_data_deleted", _capture)
    return sent


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _event(**overrides) -> dict:
    event = {
        "object_type": "athlete", "aspect_type": "update", "object_id": ATHLETE,
        "owner_id": ATHLETE, "subscription_id": 120475, "event_time": 1516126040,
        "updates": {"authorized": "false"},
    }
    event.update(overrides)
    return event


def _activity_delete(activity_id: int, owner_id: int = ATHLETE) -> dict:
    return _event(object_type="activity", aspect_type="delete", object_id=activity_id,
                  owner_id=owner_id, updates={})


def _route_ids(db_session, user) -> set[int]:
    return set(db_session.scalars(select(Route.id).where(Route.owner_id == user.id)))


@pytest.fixture
def account(db_session, users):
    """Owner: connected, two imports from activity 11 (one a copy) on a trip
    day and with a share link, one from activity 22, one drawn route.
    Other: connected with their own import."""
    owner, other = users["owner"], users["other"]
    connect(db_session, owner, ATHLETE)
    connect(db_session, other, OTHER_ATHLETE)
    a11 = make_route(db_session, owner, activity_id=11)
    a11_copy = make_route(db_session, owner, activity_id=11, name="R (copy)")
    a22 = make_route(db_session, owner, activity_id=22)
    drawn = make_route(db_session, owner)
    theirs = make_route(db_session, other, activity_id=33)
    trip = Trip(owner_id=owner.id, name="T", date=date(2026, 12, 1), region="colorado")
    db_session.add(trip)
    db_session.flush()
    db_session.add_all([
        TripRoute(trip_id=trip.id, route_id=a11.id),
        TripRoute(trip_id=trip.id, route_id=drawn.id),
        RouteShare(route_id=a11_copy.id, token="legacy-share", created_by_id=owner.id),
    ])
    db_session.flush()
    return {"a11": a11.id, "a11_copy": a11_copy.id, "a22": a22.id, "drawn": drawn.id,
            "theirs": theirs.id, "trip": trip.id}


def _conn(db_session, user):
    return db_session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()


# ── GET: subscription validation ──────────────────────────────────────────────

def test_validation_echoes_challenge(client):
    resp = client.get("/strava/webhook", params={
        "hub.mode": "subscribe", "hub.challenge": "15f7d1a91c1f40f8", "hub.verify_token": VERIFY,
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"hub.challenge": "15f7d1a91c1f40f8"}


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.challenge": "x", "hub.verify_token": "wrong"},
    {"hub.mode": "subscribe", "hub.challenge": "x"},
    {"hub.mode": "unsubscribe", "hub.challenge": "x", "hub.verify_token": VERIFY},
    {"hub.mode": "subscribe", "hub.verify_token": VERIFY},
])
def test_validation_refused(client, params):
    assert client.get("/strava/webhook", params=params).status_code == 403


def test_webhook_off_without_verify_token(client, monkeypatch, strava):
    monkeypatch.setattr(get_settings(), "strava_webhook_verify_token", "")
    resp = client.get("/strava/webhook", params={
        "hub.mode": "subscribe", "hub.challenge": "x", "hub.verify_token": "",
    })
    assert resp.status_code == 404
    assert client.post("/strava/webhook", json=_event()).status_code == 404
    assert strava.calls == []


def test_verify_token_is_redacted_in_access_log():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.5:1234", "GET",
         f"/strava/webhook?hub.verify_token={VERIFY}&hub.challenge=abc&hub.mode=subscribe", "1.1", 200),
        None,
    )
    log_redact.AccessRedactFilter().filter(record)
    line = record.getMessage()
    assert VERIFY not in line
    assert "hub.verify_token=<redacted>&hub.challenge=abc" in line


# ── POST: deauthorization ─────────────────────────────────────────────────────

def test_deauth_confirmed_by_strava_purges_strava_data_only(client, db_session, users, account, strava, emails):
    strava.athlete = 401  # our token was revoked
    resp = client.post("/strava/webhook", json=_event())
    assert resp.status_code == 200

    owner, other = users["owner"], users["other"]
    assert _conn(db_session, owner) is None
    assert _route_ids(db_session, owner) == {account["drawn"]}
    # The trip keeps its drawn route; the Strava route's trip link and the
    # copy's share link went with them.
    assert set(db_session.scalars(select(TripRoute.route_id))) == {account["drawn"]}
    assert db_session.scalars(select(RouteShare)).all() == []
    # Someone else's Strava data is untouched.
    assert _conn(db_session, other) is not None
    assert _route_ids(db_session, other) == {account["theirs"]}
    assert emails == [{"to": owner.email, "why": strava_data.WHY_REVOKED, "routes_deleted": 3}]


def test_deauth_with_expired_token_and_rejected_refresh_purges(client, db_session, users, strava):
    connect(db_session, users["owner"], ATHLETE, expired=True)
    make_route(db_session, users["owner"], activity_id=11)
    strava.refresh = 400  # Strava rejects the revoked refresh token
    assert client.post("/strava/webhook", json=_event()).status_code == 200
    assert _conn(db_session, users["owner"]) is None
    assert _route_ids(db_session, users["owner"]) == set()


def test_deauth_event_is_kept_when_our_access_still_works(client, db_session, users, account, strava, emails):
    """Anyone can post an event with a public athlete id: Strava saying the
    token works means it wasn't a real revocation."""
    strava.athlete = 200
    assert client.post("/strava/webhook", json=_event()).status_code == 200
    assert _conn(db_session, users["owner"]) is not None
    assert len(_route_ids(db_session, users["owner"])) == 4
    assert strava.calls == ["GET /athlete"]
    assert emails == []


@pytest.mark.parametrize("failure", ["down", 500, 429])
def test_deauth_is_kept_when_strava_cant_confirm(client, db_session, users, account, strava, failure):
    if failure == "down":
        strava.down = True
    else:
        strava.athlete = failure
    assert client.post("/strava/webhook", json=_event()).status_code == 200
    assert _conn(db_session, users["owner"]) is not None
    assert len(_route_ids(db_session, users["owner"])) == 4


def test_deauth_for_unknown_athlete_asks_nobody(client, db_session, users, account, strava):
    assert client.post("/strava/webhook", json=_event(owner_id=999, object_id=999)).status_code == 200
    assert strava.calls == []
    assert len(_route_ids(db_session, users["owner"])) == 4


@pytest.mark.parametrize("event", [
    _event(updates={"authorized": "true"}),
    _event(updates={"title": "x"}),
    _event(object_type="activity", aspect_type="create", object_id=11, updates={}),
    _event(object_type="activity", aspect_type="update", object_id=11, updates={"title": "x"}),
    _event(owner_id="4242"),
])
def test_other_events_are_ignored(client, db_session, users, account, strava, event):
    assert client.post("/strava/webhook", json=event).status_code == 200
    assert strava.calls == []
    assert _conn(db_session, users["owner"]) is not None
    assert len(_route_ids(db_session, users["owner"])) == 4


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_bad_body_is_400(client, body):
    resp = client.post("/strava/webhook", content=body, headers={"content-type": "application/json"})
    assert resp.status_code == 400


def test_webhook_rate_limited_per_ip(client, monkeypatch, strava):
    monkeypatch.setitem(tile_tokens.BUCKETS, "strava_webhook", (parse("2/minute"),))
    for _ in range(2):
        assert client.post("/strava/webhook", json=_event(owner_id=1)).status_code == 200
    resp = client.post("/strava/webhook", json=_event(owner_id=1))
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


# ── POST: deleted activities ──────────────────────────────────────────────────

def test_activity_delete_confirmed_removes_its_routes(client, db_session, users, account, strava, emails):
    strava.activities = {11: 404, 22: {"id": 22}}
    assert client.post("/strava/webhook", json=_activity_delete(11)).status_code == 200
    assert _route_ids(db_session, users["owner"]) == {account["a22"], account["drawn"]}
    assert _conn(db_session, users["owner"]) is not None
    assert strava.calls == ["GET /activities/11"]
    assert emails == []


def test_activity_delete_kept_when_activity_still_there(client, db_session, users, account, strava):
    strava.activities = {11: {"id": 11}}
    assert client.post("/strava/webhook", json=_activity_delete(11)).status_code == 200
    assert len(_route_ids(db_session, users["owner"])) == 4


def test_activity_delete_without_imports_asks_nobody(client, db_session, users, account, strava):
    assert client.post("/strava/webhook", json=_activity_delete(99)).status_code == 200
    assert strava.calls == []


def test_activity_delete_from_another_athlete_touches_only_theirs(client, db_session, users, account, strava):
    """Activity 11 is the owner's; an event naming it under the other
    athlete finds none of the other's routes and changes nothing."""
    strava.activities = {11: 404}
    assert client.post("/strava/webhook", json=_activity_delete(11, owner_id=OTHER_ATHLETE)).status_code == 200
    assert strava.calls == []
    assert len(_route_ids(db_session, users["owner"])) == 4


# ── DELETE /auth/strava/disconnect ────────────────────────────────────────────

def test_disconnect_deletes_imports_and_confirms(make_client, db_session, users, account, strava, emails):
    resp = make_client("owner").delete("/auth/strava/disconnect")
    assert resp.status_code == 204
    assert _conn(db_session, users["owner"]) is None
    assert _route_ids(db_session, users["owner"]) == {account["drawn"]}
    assert _route_ids(db_session, users["other"]) == {account["theirs"]}
    assert "POST https://www.strava.com/oauth/deauthorize" in strava.calls
    assert emails == [{"to": users["owner"].email, "why": strava_data.WHY_DISCONNECTED, "routes_deleted": 3}]


def test_disconnect_still_deletes_when_strava_is_down(make_client, db_session, users, account, strava):
    strava.down = True
    assert make_client("owner").delete("/auth/strava/disconnect").status_code == 204
    assert _conn(db_session, users["owner"]) is None
    assert _route_ids(db_session, users["owner"]) == {account["drawn"]}


def test_disconnect_without_strava_data_is_a_quiet_204(make_client, db_session, users, strava, emails):
    make_route(db_session, users["owner"])
    assert make_client("owner").delete("/auth/strava/disconnect").status_code == 204
    assert emails == []
    assert strava.calls == []
    assert len(_route_ids(db_session, users["owner"])) == 1


def test_disconnect_survives_a_failing_email(make_client, db_session, users, account, strava, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("mail down")

    monkeypatch.setattr("app.services.email.send_strava_data_deleted", _boom)
    assert make_client("owner").delete("/auth/strava/disconnect").status_code == 204
    assert _conn(db_session, users["owner"]) is None
