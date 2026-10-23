"""The Strava 7-day rule job (app/jobs/strava_refresh.py): imports are
re-fetched before they're 7 days old, deleted when Strava no longer has them
or can't be asked in time. Strava's HTTP API is faked."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2.shape import to_shape
from sqlalchemy import select

from app.jobs import strava_refresh
from app.models.route import Route
from app.models.strava import StravaConnection
from app.services.cog_sampler import TerrainSample
from tests import strava_fakes
from tests.strava_fakes import TRACK, connect, encode_polyline, make_route

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
FRESH = NOW - timedelta(days=2)
DUE = NOW - timedelta(days=6, hours=1)
OVERDUE = NOW - timedelta(days=8)


@pytest.fixture
def strava(monkeypatch):
    return strava_fakes.install(monkeypatch)


@pytest.fixture(autouse=True)
def emails(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("app.services.email.send_strava_data_deleted", _capture)
    return sent


@pytest.fixture
def sampled(monkeypatch) -> list[list]:
    """Fake terrain sampling; records each polyline sampled."""
    calls: list[list] = []

    def _fake(vertices, region, settings, n_total=128):
        calls.append(list(vertices))
        return [TerrainSample(distance_m=i * 10.0, elevation_m=3100.0, slope_deg=30.0, aspect_deg=0.0)
                for i in range(n_total)]

    monkeypatch.setattr("app.routers.routes.sample_polyline", _fake)
    return calls


def _detail(track=TRACK) -> dict:
    return {"id": 1, "map": {"polyline": encode_polyline(track)}}


def _run(db_session, **kwargs):
    return asyncio.run(strava_refresh.refresh(db_session, now=NOW, **kwargs))


def _ids(db_session, user) -> set[int]:
    return set(db_session.scalars(select(Route.id).where(Route.owner_id == user.id)))


def test_due_route_is_refetched_and_kept(db_session, users, strava, sampled):
    connect(db_session, users["owner"], 1)
    route = make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    copy = make_route(db_session, users["owner"], activity_id=11, fetched_at=FRESH)
    strava.activities = {11: _detail()}

    counts = _run(db_session)

    assert strava.calls == ["GET /activities/11"]
    db_session.refresh(route)
    db_session.refresh(copy)
    # Both copies of the activity were just fetched; the track is unchanged,
    # so no terrain was sampled.
    assert route.strava_fetched_at == NOW and copy.strava_fetched_at == NOW
    assert sampled == []
    assert counts["refreshed"] == 2 and counts["expired"] == 0


def test_fresh_and_drawn_routes_are_left_alone(db_session, users, strava):
    connect(db_session, users["owner"], 1)
    fresh = make_route(db_session, users["owner"], activity_id=11, fetched_at=FRESH)
    drawn = make_route(db_session, users["owner"])
    assert sum(_run(db_session).values()) == 0
    assert strava.calls == []
    assert _ids(db_session, users["owner"]) == {fresh.id, drawn.id}


def test_changed_track_is_resampled(db_session, users, strava, sampled):
    connect(db_session, users["owner"], 1)
    route = make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    cropped = TRACK[:2]
    strava.activities = {11: _detail(cropped)}

    counts = _run(db_session)

    assert counts["resampled"] == 1
    assert sampled == [cropped]
    db_session.refresh(route)
    assert [c[:2] for c in to_shape(route.geom).coords] == [pytest.approx(c) for c in cropped]
    assert route.samples[0]["elevation_m"] == 3100.0


def test_activity_gone_deletes_its_routes(db_session, users, strava):
    connect(db_session, users["owner"], 1)
    make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    make_route(db_session, users["owner"], activity_id=11, fetched_at=FRESH)  # a copy
    keep = make_route(db_session, users["owner"], activity_id=22, fetched_at=DUE)
    strava.activities = {11: 404, 22: _detail()}

    counts = _run(db_session)

    assert counts["gone"] == 2
    assert _ids(db_session, users["owner"]) == {keep.id}


def test_revoked_access_purges_and_confirms(db_session, users, strava, emails):
    connect(db_session, users["owner"], 1, expired=True)
    make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    drawn = make_route(db_session, users["owner"])
    strava.refresh = 400  # the athlete revoked whumpf and we missed the webhook

    counts = _run(db_session)

    assert counts["revoked_users"] == 1
    assert _ids(db_session, users["owner"]) == {drawn.id}
    assert db_session.scalars(select(StravaConnection)).all() == []
    assert len(emails) == 1 and emails[0]["routes_deleted"] == 1


def test_no_connection_deletes_imports(db_session, users, strava):
    make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    assert _run(db_session)["no_connection"] == 1
    assert _ids(db_session, users["owner"]) == set()


def test_unreachable_strava_keeps_due_routes_but_expires_overdue(db_session, users, strava):
    connect(db_session, users["owner"], 1)
    due = make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    make_route(db_session, users["owner"], activity_id=22, fetched_at=OVERDUE)
    strava.down = True

    counts = _run(db_session)

    assert counts["unknown"] == 2 and counts["expired"] == 1
    assert _ids(db_session, users["owner"]) == {due.id}


def test_rate_limit_stops_the_run(db_session, users, strava):
    connect(db_session, users["owner"], 1)
    make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE)
    make_route(db_session, users["owner"], activity_id=22, fetched_at=DUE)
    strava.activities = {11: 429, 22: _detail()}

    counts = _run(db_session)

    assert counts["rate_limited"] == 1
    assert strava.calls == ["GET /activities/11"]  # never asked for 22


def test_limit_caps_calls_oldest_first(db_session, users, strava):
    connect(db_session, users["owner"], 1)
    older = make_route(db_session, users["owner"], activity_id=11, fetched_at=DUE - timedelta(hours=5))
    newer = make_route(db_session, users["owner"], activity_id=22, fetched_at=DUE)
    strava.activities = {11: _detail(), 22: _detail()}

    _run(db_session, limit=1)

    assert strava.calls == ["GET /activities/11"]
    db_session.refresh(older)
    db_session.refresh(newer)
    assert older.strava_fetched_at == NOW and newer.strava_fetched_at == DUE


def test_overdue_route_that_refreshes_is_kept(db_session, users, strava):
    """A route already past 7 days (e.g. imported before this job existed)
    is kept if this run can fetch it."""
    connect(db_session, users["owner"], 1)
    route = make_route(db_session, users["owner"], activity_id=11, fetched_at=OVERDUE)
    strava.activities = {11: _detail()}
    counts = _run(db_session)
    assert counts["refreshed"] == 1 and counts["expired"] == 0
    assert _ids(db_session, users["owner"]) == {route.id}


def test_main_exit_code_and_dry_run(db_session, users, strava, monkeypatch, capsys):
    @contextmanager
    def _shared():
        yield db_session

    monkeypatch.setattr(strava_refresh, "job_session", _shared)
    connect(db_session, users["owner"], 1)
    make_route(db_session, users["owner"], activity_id=11,
               fetched_at=datetime.now(UTC) - timedelta(days=6, hours=1))

    assert strava_refresh.main(["--dry-run"]) == 0
    assert "1 imported routes due (1 Strava calls)" in capsys.readouterr().out
    assert strava.calls == []

    strava.down = True
    assert strava_refresh.main([]) == 1  # Strava unreachable: the cron log shows it
