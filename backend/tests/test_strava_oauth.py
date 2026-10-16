"""Strava OAuth start/callback: return_to allowlist, signed state, redirects.

The token exchange is mocked; nothing talks to Strava.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from jose import jwt
from sqlalchemy import select

from app.auth.service import create_access_token, decode_token
from app.config import get_settings
from app.models.strava import StravaConnection
from app.models.user import User

_TOKEN_DATA = {
    "access_token": "acc",
    "refresh_token": "ref",
    "expires_at": 2_000_000_000,
    "scope": "read,activity:read_all",
    "athlete": {"id": 4242, "firstname": "Ada", "lastname": "L"},
}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "strava_return_to_allow_dev", False)
    monkeypatch.setattr(s, "strava_success_url", "https://old.example")

    async def _exchange(code):
        if code == "bad":
            raise RuntimeError("strava said no")
        return _TOKEN_DATA

    monkeypatch.setattr("app.services.strava.exchange_code", _exchange)


@pytest.fixture
def allow_dev(monkeypatch):
    monkeypatch.setattr(get_settings(), "strava_return_to_allow_dev", True)


def _start(client, return_to=None):
    params = {} if return_to is None else {"return_to": return_to}
    return client.get("/auth/strava/authorize", params=params)


def _state(client, return_to=None) -> str:
    resp = _start(client, return_to)
    assert resp.status_code == 200, resp.text
    return parse_qs(urlsplit(resp.json()["url"]).query)["state"][0]


def _callback(client, **params):
    return client.get("/auth/strava/callback", params=params, follow_redirects=False)


def _location(resp) -> tuple[str, dict[str, list[str]]]:
    assert resp.status_code == 302, resp.text
    parts = urlsplit(resp.headers["location"])
    base = parts._replace(query="", fragment="").geturl()
    return base, parse_qs(parts.query)


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "whumpf://strava",
        "whumpf://settings/strava?tab=1",
        "https://whumpf.co",
        "https://whumpf.co/",
        "https://whumpf.co/settings?x=1#frag",
    ],
)
def test_allowed_return_to(make_client, url):
    assert _start(make_client(), url).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://evil.com/",
        "http://whumpf.co/",
        "https://whumpf.co.evil.com/",
        "https://evil.com/https://whumpf.co/",
        "https://whumpf.co@evil.com/",
        "https://user@whumpf.co/",
        "https://whumpf.co:444/",
        "https://WHUMPF.CO/",
        "https://sub.whumpf.co/",
        "https://whumpf.co\\@evil.com/",
        "//whumpf.co/",
        "whumpf:strava",
        "javascript:alert(1)",
        "https://whumpf.co/\nSet-Cookie:x",
        "https://whumpf.co/ x",
        "https://whumpf.co/" + "a" * 2100,
        # dev-only targets are refused while the setting is off
        "exp://192.168.1.5:8081/--/strava",
        "http://localhost:8081/strava",
    ],
)
def test_rejected_return_to(make_client, url):
    resp = _start(make_client(), url)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "return_to is not allowed"


@pytest.mark.parametrize(
    "url", ["exp://192.168.1.5:8081/--/strava", "http://localhost:8081/", "http://localhost:8081"]
)
def test_dev_return_to_allowed_with_setting(make_client, allow_dev, url):
    assert _start(make_client(), url).status_code == 200


@pytest.mark.parametrize(
    "url", ["http://localhost:8082/", "https://localhost:8081/", "http://127.0.0.1:8081/"]
)
def test_dev_return_to_still_exact(make_client, allow_dev, url):
    assert _start(make_client(), url).status_code == 400


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def test_state_is_not_a_login_token(make_client):
    state = _state(make_client(), "whumpf://strava")
    assert decode_token(state) is None


def test_login_token_is_not_a_state(make_client, users):
    client = make_client()
    resp = _callback(client, code="ok", state=create_access_token(users["owner"].email))
    assert resp.status_code == 400


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_tampered_return_to_is_rejected(make_client):
    client = make_client()
    header, _payload, sig = _state(client, "whumpf://strava").split(".")
    claims = jwt.get_unverified_claims(f"{header}.{_payload}.{sig}")
    claims["rt"] = "https://evil.com/"
    forged = ".".join([header, _b64(claims), sig])
    resp = _callback(client, code="ok", state=forged)
    assert resp.status_code == 400
    assert "evil" not in resp.headers.get("location", "")


def test_state_signed_with_other_key_is_rejected(make_client, users):
    exp = datetime.now(UTC) + timedelta(minutes=5)
    forged = jwt.encode(
        {"purpose": "strava_oauth", "uid": users["owner"].id, "rt": "whumpf://x", "exp": exp},
        "not-the-secret",
        algorithm="HS256",
    )
    assert _callback(make_client(), code="ok", state=forged).status_code == 400


def test_wrong_purpose_is_rejected(make_client, users):
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(minutes=5)
    other = jwt.encode(
        {"purpose": "something_else", "uid": users["owner"].id, "rt": "whumpf://x", "exp": exp},
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert _callback(make_client(), code="ok", state=other).status_code == 400


def test_state_with_disallowed_return_to_is_rejected(make_client, allow_dev, monkeypatch):
    """Validly signed while the dev setting was on; the setting is off by callback."""
    client = make_client()
    state = _state(client, "exp://192.168.1.5:8081/--/strava")
    monkeypatch.setattr(get_settings(), "strava_return_to_allow_dev", False)
    assert _callback(client, code="ok", state=state).status_code == 400


def test_return_to_in_callback_query_is_ignored(make_client):
    client = make_client()
    state = _state(client)  # no return_to
    resp = _callback(client, code="ok", state=state, return_to="https://evil.com/")
    assert _location(resp) == ("https://old.example", {"strava": ["connected"]})


# --------------------------------------------------------------------------- #
# Redirects with return_to
# --------------------------------------------------------------------------- #
def test_connected_redirects_to_return_to(make_client, db_session, users):
    client = make_client()
    state = _state(client, "whumpf://strava")
    resp = _callback(client, code="ok", state=state)
    assert _location(resp) == ("whumpf://strava", {"strava": ["connected"]})

    conn = db_session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == users["owner"].id)
    ).one()
    assert conn.strava_athlete_id == 4242


def test_connected_keeps_existing_query_and_fragment(make_client):
    client = make_client()
    state = _state(client, "https://whumpf.co/settings?tab=apps&strava=stale#top")
    resp = _callback(client, code="ok", state=state)
    loc = resp.headers["location"]
    assert loc == "https://whumpf.co/settings?tab=apps&strava=connected#top"


def test_denied_redirects_to_return_to(make_client):
    client = make_client()
    state = _state(client, "whumpf://strava")
    resp = _callback(client, error="access_denied", state=state)
    assert _location(resp) == ("whumpf://strava", {"strava": ["error"], "reason": ["denied"]})


def test_token_exchange_failure_redirects_to_return_to(make_client):
    client = make_client()
    state = _state(client, "whumpf://strava")
    resp = _callback(client, code="bad", state=state)
    assert _location(resp) == (
        "whumpf://strava",
        {"strava": ["error"], "reason": ["token_exchange_failed"]},
    )


def test_expired_state_redirects_to_return_to(make_client, monkeypatch, db_session, users):
    monkeypatch.setattr("app.routers.strava._STATE_TTL", timedelta(seconds=-1))
    client = make_client()
    state = _state(client, "whumpf://strava")
    resp = _callback(client, code="ok", state=state)
    assert _location(resp) == ("whumpf://strava", {"strava": ["error"], "reason": ["expired"]})
    # Nothing was connected.
    assert db_session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == users["owner"].id)
    ).first() is None


def test_deleted_user_redirects_bad_state(make_client, db_session, users):
    client = make_client()
    state = _state(client, "whumpf://strava")
    db_session.delete(db_session.get(User, users["owner"].id))
    db_session.flush()
    resp = _callback(client, code="ok", state=state)
    assert _location(resp) == ("whumpf://strava", {"strava": ["error"], "reason": ["bad_state"]})


# --------------------------------------------------------------------------- #
# Legacy redirects (no return_to)
# --------------------------------------------------------------------------- #
def test_legacy_connected(make_client):
    client = make_client()
    resp = _callback(client, code="ok", state=_state(client))
    assert resp.headers["location"] == "https://old.example?strava=connected"


def test_legacy_denied(make_client):
    client = make_client()
    resp = _callback(client, error="access_denied", state=_state(client))
    assert resp.headers["location"] == "https://old.example?strava=denied"


def test_legacy_missing_state_is_denied(make_client):
    resp = _callback(make_client(), code="ok")
    assert resp.headers["location"] == "https://old.example?strava=denied"


def test_legacy_token_exchange_failure(make_client):
    client = make_client()
    resp = _callback(client, code="bad", state=_state(client))
    assert resp.headers["location"] == "https://old.example?strava=error"


def test_legacy_expired_state_is_400(make_client, monkeypatch):
    monkeypatch.setattr("app.routers.strava._STATE_TTL", timedelta(seconds=-1))
    client = make_client()
    assert _callback(client, code="ok", state=_state(client)).status_code == 400


def test_legacy_garbage_state_is_400(make_client):
    assert _callback(make_client(), code="ok", state="garbage").status_code == 400
