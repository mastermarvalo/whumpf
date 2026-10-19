"""GET /status: the 30 s service-check cache, banners that bypass it, and
the per-IP `status` limit. The service checks themselves are tested with
each layer (test_snowfall.py, test_roads.py, ...)."""

from __future__ import annotations

import pytest

from app.routers import status


@pytest.fixture
def checks(monkeypatch) -> list[int]:
    """Fake service checks; returns a list that grows by one per run."""
    runs: list[int] = []

    async def fake(session):
        runs.append(1)
        return [status.ServiceHealth(name="api", healthy=True)]

    monkeypatch.setattr(status, "_check_services", fake)
    return runs


@pytest.fixture
def admin_client(make_client, users, db_session):
    users["owner"].is_admin = True
    db_session.flush()
    return make_client("owner")


def test_service_checks_cached(make_client, checks, monkeypatch):
    client = make_client()
    for _ in range(3):
        resp = client.get("/status")
        assert resp.status_code == 200
        assert resp.json()["overall"] == "operational"
    assert len(checks) == 1

    clock = status.time.monotonic() + status.SERVICES_TTL_S + 1
    monkeypatch.setattr(status.time, "monotonic", lambda: clock)
    client.get("/status")
    assert len(checks) == 2


def test_banner_shows_at_once_despite_cache(admin_client, checks):
    assert admin_client.get("/status").json()["active_banner"] is None

    posted = admin_client.post("/status", json={"title": "Tiles down", "severity": "critical"})
    assert posted.status_code == 201
    body = admin_client.get("/status").json()
    assert body["active_banner"]["title"] == "Tiles down"
    assert body["overall"] == "outage"

    banner_id = posted.json()["id"]
    assert admin_client.post(f"/status/{banner_id}/resolve").status_code == 200
    body = admin_client.get("/status").json()
    assert body["active_banner"] is None
    assert body["overall"] == "operational"
    assert body["recent_banners"][0]["resolved_at"] is not None

    assert admin_client.delete(f"/status/{banner_id}").status_code == 204
    assert admin_client.get("/status").json()["recent_banners"] == []
    assert len(checks) == 1


def test_status_rate_limited_per_ip(make_client, checks):
    client = make_client()
    for _ in range(60):
        assert client.get("/status").status_code == 200
    resp = client.get("/status")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
