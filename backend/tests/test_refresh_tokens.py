"""Refresh tokens: issue on login/register, rotation, reuse detection with a
grace window, logout, expiry, and revocation on password reset / account
deletion. These go through the real login, not the get_current_user override.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.codes import hash_link_token
from app.auth.service import SESSION_COOKIE, _hash_refresh_token, hash_password
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import RefreshToken, User

PASSWORD = "correct horse battery"


@pytest.fixture
def user(db_session) -> User:
    u = User(email="refresh@example.com", hashed_password=hash_password(PASSWORD),
             email_verified=True)
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def client(db_session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _login(client: TestClient) -> dict:
    r = client.post("/auth/token", data={"username": "refresh@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.cookies.clear()  # exercise the bearer/refresh path, not the cookie
    return r.json()


def _refresh(client: TestClient, token: str):
    return client.post("/auth/refresh", json={"refresh_token": token})


def _row(db_session, raw: str) -> RefreshToken:
    return db_session.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_refresh_token(raw)),
    ).one()


def _me(client: TestClient, access: str):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})


def test_login_returns_refresh_token_stored_hashed(client, db_session, user):
    body = _login(client)
    raw = body["refresh_token"]
    row = _row(db_session, raw)
    assert row.user_id == user.id
    assert row.token_hash != raw
    assert row.revoked_at is None
    assert row.expires_at - datetime.now(timezone.utc) > timedelta(days=59)


def test_login_still_sets_session_cookie(client, user):
    # https: the cookie is Secure outside WHUMPF_ENV=dev
    browser = TestClient(app, base_url="https://testserver")
    r = browser.post("/auth/token", data={"username": user.email, "password": PASSWORD})
    assert r.status_code == 200
    assert SESSION_COOKIE in r.cookies
    assert browser.get("/auth/me").json()["email"] == user.email


def test_register_returns_refresh_token(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "skip_email_verification", True)
    r = client.post("/auth/register", json={"email": "new@example.com", "password": PASSWORD})
    assert r.status_code == 201, r.text
    client.cookies.clear()
    r = _refresh(client, r.json()["refresh_token"])
    assert r.status_code == 200
    assert _me(client, r.json()["access_token"]).json()["email"] == "new@example.com"


def test_refresh_rotates(client, db_session, user):
    old = _login(client)["refresh_token"]
    r = _refresh(client, old)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refresh_token"] != old
    assert _me(client, body["access_token"]).json()["email"] == user.email
    old_row, new_row = _row(db_session, old), _row(db_session, body["refresh_token"])
    assert old_row.revoked_at is not None
    assert old_row.rotated_at is not None
    assert new_row.revoked_at is None
    assert new_row.family_id == old_row.family_id

    # the new one rotates again
    assert _refresh(client, body["refresh_token"]).status_code == 200


def test_reuse_outside_grace_revokes_everything(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    other_device = _login(client)["refresh_token"]
    t2 = _refresh(client, t1).json()["refresh_token"]
    _row(db_session, t1).rotated_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db_session.flush()

    assert _refresh(client, t1).status_code == 401
    assert _refresh(client, t2).status_code == 401
    assert _refresh(client, other_device).status_code == 401
    live = db_session.scalars(select(RefreshToken).where(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None),
    )).all()
    assert live == []


def test_reuse_inside_grace_issues_fresh_token(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    t2 = _refresh(client, t1).json()["refresh_token"]

    # same token again within 60 s (lost response / racing request)
    r = _refresh(client, t1)
    assert r.status_code == 200, r.text
    t3 = r.json()["refresh_token"]
    assert t3 not in (t1, t2)
    assert _me(client, r.json()["access_token"]).status_code == 200
    assert _row(db_session, t3).family_id == _row(db_session, t1).family_id

    # nothing got revoked: both the earlier and the new token still work
    assert _refresh(client, t2).status_code == 200
    assert _refresh(client, t3).status_code == 200


def test_grace_does_not_apply_after_logout(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    t2 = _refresh(client, t1).json()["refresh_token"]
    assert client.post("/auth/logout", json={"refresh_token": t2}).status_code == 204
    assert _refresh(client, t1).status_code == 401


def test_logout_revokes_refresh_token(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    other_device = _login(client)["refresh_token"]
    r = client.post("/auth/logout", json={"refresh_token": t1})
    assert r.status_code == 204
    row = _row(db_session, t1)
    assert row.revoked_at is not None
    assert row.rotated_at is None
    # a logged-out token is just rejected, not treated as reuse...
    assert _refresh(client, t1).status_code == 401
    assert _refresh(client, t1).status_code == 401
    # ...so other sessions survive
    assert _refresh(client, other_device).status_code == 200


@pytest.mark.parametrize("kwargs", [{}, {"json": {}}, {"json": {"refresh_token": "nope"}}])
def test_logout_without_valid_token_still_clears_cookie(client, user, kwargs):
    client.post("/auth/token", data={"username": user.email, "password": PASSWORD})
    r = client.post("/auth/logout", **kwargs)
    assert r.status_code == 204
    assert SESSION_COOKIE in r.headers.get("set-cookie", "")


def test_expired_refresh_token_rejected(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    _row(db_session, t1).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.flush()
    assert _refresh(client, t1).status_code == 401


def test_unknown_refresh_token_rejected(client, user):
    assert _refresh(client, "not-a-token").status_code == 401


def test_inactive_user_cannot_refresh(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    user.is_active = False
    db_session.flush()
    assert _refresh(client, t1).status_code == 401


def test_password_reset_revokes_all(client, db_session, user):
    t1 = _login(client)["refresh_token"]
    t2 = _login(client)["refresh_token"]
    user.password_reset_token = hash_link_token("reset-tok")
    user.password_reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.flush()

    r = client.post("/auth/password-reset/confirm",
                    json={"token": "reset-tok", "new_password": "another password"})
    assert r.status_code == 204
    assert _refresh(client, t1).status_code == 401
    assert _refresh(client, t2).status_code == 401


def test_delete_me_removes_refresh_tokens(client, db_session, user):
    body = _login(client)
    _login(client)
    user_id = user.id
    r = client.request("DELETE", "/auth/me", json={"password": PASSWORD},
                       headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r.status_code == 204
    assert db_session.scalars(
        select(RefreshToken).where(RefreshToken.user_id == user_id),
    ).all() == []


def test_refresh_rate_limited_per_ip(client, user):
    codes = [_refresh(client, "not-a-token").status_code for _ in range(31)]
    assert codes[:30] == [401] * 30
    assert codes[30] == 429
