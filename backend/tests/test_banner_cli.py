"""`make banner` (app/jobs/banner.py): post, list and resolve outage banners
from the VM, straight to the database, and what GET /status then shows."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import select

from app.jobs import banner
from app.models.status import StatusBanner
from app.routers import status


@pytest.fixture(autouse=True)
def job_db(monkeypatch, db_session):
    @contextmanager
    def _shared():
        yield db_session

    monkeypatch.setattr(banner, "job_session", _shared)


@pytest.fixture(autouse=True)
def checks(monkeypatch):
    async def fake(session):
        return [status.ServiceHealth(name="api", healthy=True)]

    monkeypatch.setattr(status, "_check_services", fake)


def _banners(db_session) -> list[StatusBanner]:
    return list(db_session.scalars(select(StatusBanner).order_by(StatusBanner.id)))


def test_post_shows_on_status_and_resolve_clears_it(db_session, make_client, capsys):
    assert banner.main(["post", "--severity", "critical", "--title", "  Tiles are down  ",
                        "--body", "Working on it."]) == 0
    [b] = _banners(db_session)
    assert (b.title, b.body, b.severity, b.created_by_id, b.resolved_at) == (
        "Tiles are down", "Working on it.", "critical", None, None)
    assert f"Posted banner {b.id}" in capsys.readouterr().out

    body = make_client().get("/status").json()
    assert body["active_banner"]["title"] == "Tiles are down"
    assert body["overall"] == "outage"

    assert banner.main(["resolve", str(b.id)]) == 0
    assert f"Resolved banner {b.id}." in capsys.readouterr().out
    body = make_client().get("/status").json()
    assert body["active_banner"] is None
    assert body["overall"] == "operational"


def test_severity_defaults_to_minor(db_session):
    assert banner.main(["post", "--title", "Radar is late"]) == 0
    assert _banners(db_session)[0].severity == "minor"


def test_list(db_session, capsys):
    banner.main(["post", "--title", "Old"])
    old = _banners(db_session)[0]
    banner.main(["resolve", str(old.id)])
    banner.main(["post", "--severity", "major", "--title", "Snowfall stale", "--body", "NOAA late"])
    capsys.readouterr()

    assert banner.main([]) == 0
    out = capsys.readouterr().out
    assert out.index("Active (1") < out.index("Snowfall stale") < out.index("NOAA late")
    assert out.index("Recently resolved:") < out.index("Old")

    assert banner.main(["list"]) == 0
    assert capsys.readouterr().out == out


def test_list_with_nothing(capsys):
    assert banner.main([]) == 0
    assert capsys.readouterr().out == "No active banner.\n"


@pytest.mark.parametrize("title", ["   ", "x" * 201])
def test_bad_title_posts_nothing(db_session, capsys, title):
    assert banner.main(["post", "--title", title]) == 2
    assert capsys.readouterr().err.startswith("title ")
    assert _banners(db_session) == []


def test_bad_severity_is_refused_by_argparse(db_session):
    with pytest.raises(SystemExit) as exc:
        banner.main(["post", "--title", "x", "--severity", "apocalyptic"])
    assert exc.value.code == 2
    assert _banners(db_session) == []


def test_resolve_unknown_or_twice(db_session, capsys):
    assert banner.main(["resolve", "999999"]) == 1
    assert "No banner 999999" in capsys.readouterr().err
    banner.main(["post", "--title", "x"])
    b = _banners(db_session)[0]
    banner.main(["resolve", str(b.id)])
    first = b.resolved_at
    capsys.readouterr()
    assert banner.main(["resolve", str(b.id)]) == 0
    assert "already resolved" in capsys.readouterr().out
    assert b.resolved_at == first


def test_admin_endpoint_refuses_overlong_title_with_400(make_client, users, db_session):
    """Was a 500: the column is 200 characters and nothing checked it."""
    users["owner"].is_admin = True
    db_session.flush()
    resp = make_client("owner").post("/status", json={"title": "x" * 201})
    assert resp.status_code == 400
    assert "200" in resp.json()["detail"]
    assert make_client("owner").post("/status", json={"title": "ok", "severity": "nope"}).status_code == 400
