"""6-digit email codes for verify-email, password reset and change-email;
change-password; the legacy link flow kept for the old website. These go
through the real login, not the get_current_user override.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.auth as auth_router
from app.auth.codes import MAX_ATTEMPTS, hash_link_token
from app.auth.service import _hash_refresh_token, hash_password, verify_password
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import CodePurpose, EmailCode, RefreshToken, Trip, TripMember, User

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long password"


@pytest.fixture
def outbox(monkeypatch) -> list[dict]:
    """Captures every email the auth router sends."""
    sent: list[dict] = []

    def _capture(kind: str):
        async def _send(**kwargs):
            sent.append({"kind": kind, **kwargs})
        return _send

    for name in ("send_verification_email", "send_password_reset_email",
                 "send_email_change_code", "send_security_notice"):
        monkeypatch.setattr(auth_router, name, _capture(name))
    return sent


@pytest.fixture
def client(db_session, outbox) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def user(db_session) -> User:
    u = User(email="codes@example.com", hashed_password=hash_password(PASSWORD),
             email_verified=True)
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def unverified(db_session) -> User:
    u = User(email="unverified@example.com", hashed_password=hash_password(PASSWORD))
    db_session.add(u)
    db_session.flush()
    return u


def _last(outbox: list[dict], kind: str) -> dict:
    return [m for m in outbox if m["kind"] == kind][-1]


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


def _login(client: TestClient, email: str = "codes@example.com", password: str = PASSWORD) -> dict:
    r = client.post("/auth/token", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()  # exercise the bearer path
    return r.json()


def _auth(access: str) -> dict:
    return {"Authorization": f"Bearer {access}"}


def _code_row(db_session, user_id: int, purpose: CodePurpose) -> EmailCode:
    return db_session.scalars(
        select(EmailCode).where(EmailCode.user_id == user_id, EmailCode.purpose == purpose),
    ).one()


# ── verify email ──────────────────────────────────────────────────────────────

def test_register_sends_code_and_link_stored_hashed(client, db_session, outbox):
    r = client.post("/auth/register", json={"email": "New@Example.com", "password": PASSWORD})
    assert r.status_code == 201
    mail = _last(outbox, "send_verification_email")
    assert mail["to"] == "new@example.com"
    code, token = mail["code"], mail["token"]
    assert len(code) == 6 and code.isdigit()

    u = db_session.scalars(select(User).where(User.email == "new@example.com")).one()
    row = _code_row(db_session, u.id, CodePurpose.verify_email)
    assert code not in row.code_hash and row.code_hash != code
    assert timedelta(minutes=14) < row.expires_at - datetime.now(timezone.utc) <= timedelta(minutes=15)
    assert u.email_verification_token == hash_link_token(token)
    assert token not in u.email_verification_token


def test_verify_with_code_clears_link_too(client, db_session, outbox, unverified):
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    mail = _last(outbox, "send_verification_email")
    r = client.post("/auth/verify-email/confirm",
                    json={"email": "UNVERIFIED@example.com ", "code": mail["code"]})
    assert r.status_code == 204, r.text
    db_session.refresh(unverified)
    assert unverified.email_verified and unverified.email_verification_token is None
    r = client.post("/auth/verify-email/confirm", json={"token": mail["token"]})
    assert r.status_code == 400
    # Consumed: the same code doesn't work twice.
    r = client.post("/auth/verify-email/confirm",
                    json={"email": unverified.email, "code": mail["code"]})
    assert r.status_code == 400


def test_verify_with_legacy_link_clears_code(client, db_session, outbox, unverified):
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    mail = _last(outbox, "send_verification_email")
    assert client.post("/auth/verify-email/confirm", json={"token": mail["token"]}).status_code == 204
    db_session.refresh(unverified)
    assert unverified.email_verified
    r = client.post("/auth/verify-email/confirm",
                    json={"email": unverified.email, "code": mail["code"]})
    assert r.status_code == 400


def test_new_request_replaces_old_code(client, outbox, unverified):
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    first = _last(outbox, "send_verification_email")["code"]
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    second = _last(outbox, "send_verification_email")["code"]
    if first != second:
        r = client.post("/auth/verify-email/confirm",
                        json={"email": unverified.email, "code": first})
        assert r.status_code == 400
    r = client.post("/auth/verify-email/confirm", json={"email": unverified.email, "code": second})
    assert r.status_code == 204


def test_expired_code_rejected(client, db_session, outbox, unverified):
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    code = _last(outbox, "send_verification_email")["code"]
    row = _code_row(db_session, unverified.id, CodePurpose.verify_email)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.flush()
    r = client.post("/auth/verify-email/confirm", json={"email": unverified.email, "code": code})
    assert r.status_code == 400


def test_code_burned_after_max_attempts(client, db_session, outbox, unverified):
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    code = _last(outbox, "send_verification_email")["code"]
    for _ in range(MAX_ATTEMPTS):
        r = client.post("/auth/verify-email/confirm",
                        json={"email": unverified.email, "code": _wrong(code)})
        assert r.status_code == 400
    assert _code_row(db_session, unverified.id, CodePurpose.verify_email).attempts == MAX_ATTEMPTS
    r = client.post("/auth/verify-email/confirm", json={"email": unverified.email, "code": code})
    assert r.status_code == 400
    db_session.refresh(unverified)
    assert not unverified.email_verified


def test_code_bound_to_purpose_and_user(client, db_session, outbox, user, unverified):
    client.post("/auth/password-reset/request", json={"email": unverified.email})
    reset_code = _last(outbox, "send_password_reset_email")["code"]
    r = client.post("/auth/verify-email/confirm",
                    json={"email": unverified.email, "code": reset_code})
    assert r.status_code == 400
    # Another user's code doesn't work for this user.
    r = client.post("/auth/password-reset/confirm",
                    json={"email": user.email, "code": reset_code, "new_password": NEW_PASSWORD})
    assert r.status_code == 400


def test_unknown_email_confirm_looks_like_wrong_code(client, outbox, unverified):
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    code = _last(outbox, "send_verification_email")["code"]
    unknown = client.post("/auth/verify-email/confirm",
                          json={"email": "nobody@example.com", "code": code})
    wrong = client.post("/auth/verify-email/confirm",
                        json={"email": unverified.email, "code": _wrong(code)})
    assert unknown.status_code == wrong.status_code == 400
    assert unknown.json() == wrong.json()


def test_request_is_silent_for_unknown_and_verified(client, outbox, user):
    for email in ("nobody@example.com", user.email):
        assert client.post("/auth/verify-email/request", json={"email": email}).status_code == 204
    assert outbox == []


def test_confirm_needs_code_or_token(client):
    assert client.post("/auth/verify-email/confirm", json={"email": "a@b.c"}).status_code == 422
    assert client.post("/auth/verify-email/confirm", json={}).status_code == 422


def test_legacy_links_off(client, db_session, outbox, unverified, monkeypatch):
    monkeypatch.setattr(get_settings(), "legacy_email_links", False)
    client.post("/auth/verify-email/request", json={"email": unverified.email})
    assert _last(outbox, "send_verification_email")["token"] is None
    unverified.email_verification_token = hash_link_token("old-link")
    unverified.email_verification_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.flush()
    assert client.post("/auth/verify-email/confirm", json={"token": "old-link"}).status_code == 400


# ── rate limits ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", ["nobody@example.com", "unverified@example.com"])
def test_send_rate_limited_per_email(client, unverified, email):
    path = "/auth/password-reset/request"
    codes = [client.post(path, json={"email": email}).status_code for _ in range(4)]
    assert codes == [204, 204, 204, 429]


def test_send_rate_limited_per_ip(client):
    codes = [
        client.post("/auth/password-reset/request", json={"email": f"u{i}@example.com"}).status_code
        for i in range(6)
    ]
    assert codes == [204] * 5 + [429]


def test_check_rate_limited_per_email_across_codes(client, outbox, unverified):
    """New codes don't buy more guesses: 10/hour per address overall."""
    statuses = []
    for _ in range(3):
        client.post("/auth/verify-email/request", json={"email": unverified.email})
        code = _last(outbox, "send_verification_email")["code"]
        for _ in range(4):
            statuses.append(client.post(
                "/auth/verify-email/confirm",
                json={"email": unverified.email, "code": _wrong(code)},
            ).status_code)
    assert statuses[:10] == [400] * 10
    assert statuses[10:] == [429, 429]


# ── password reset ────────────────────────────────────────────────────────────

def test_password_reset_with_code(client, db_session, outbox, user):
    t = _login(client)["refresh_token"]
    client.post("/auth/password-reset/request", json={"email": user.email})
    mail = _last(outbox, "send_password_reset_email")
    r = client.post("/auth/password-reset/confirm",
                    json={"email": user.email, "code": mail["code"], "new_password": NEW_PASSWORD})
    assert r.status_code == 204, r.text
    db_session.refresh(user)
    assert verify_password(NEW_PASSWORD, user.hashed_password)
    assert user.password_reset_token is None
    assert client.post("/auth/refresh", json={"refresh_token": t}).status_code == 401
    assert _last(outbox, "send_security_notice")["to"] == user.email
    # The link from the same email is now dead.
    r = client.post("/auth/password-reset/confirm",
                    json={"token": mail["token"], "new_password": "yet another one"})
    assert r.status_code == 400


def test_password_reset_with_legacy_link(client, db_session, outbox, user):
    client.post("/auth/password-reset/request", json={"email": user.email})
    mail = _last(outbox, "send_password_reset_email")
    r = client.post("/auth/password-reset/confirm",
                    json={"token": mail["token"], "new_password": NEW_PASSWORD})
    assert r.status_code == 204
    db_session.refresh(user)
    assert verify_password(NEW_PASSWORD, user.hashed_password)
    assert db_session.scalars(select(EmailCode).where(EmailCode.user_id == user.id)).all() == []


def test_password_reset_short_password_costs_no_attempt(client, db_session, outbox, user):
    client.post("/auth/password-reset/request", json={"email": user.email})
    code = _last(outbox, "send_password_reset_email")["code"]
    r = client.post("/auth/password-reset/confirm",
                    json={"email": user.email, "code": code, "new_password": "short"})
    assert r.status_code == 400
    assert _code_row(db_session, user.id, CodePurpose.password_reset).attempts == 0


# ── change password ───────────────────────────────────────────────────────────

def test_change_password_keeps_own_family_revokes_others(client, db_session, outbox, user):
    mine = _login(client)
    other = _login(client)["refresh_token"]
    r = client.post("/auth/change-password", headers=_auth(mine["access_token"]), json={
        "current_password": PASSWORD, "new_password": NEW_PASSWORD,
        "refresh_token": mine["refresh_token"],
    })
    assert r.status_code == 204, r.text
    assert client.post("/auth/refresh", json={"refresh_token": other}).status_code == 401
    assert client.post("/auth/refresh",
                       json={"refresh_token": mine["refresh_token"]}).status_code == 200
    db_session.refresh(user)
    assert verify_password(NEW_PASSWORD, user.hashed_password)
    assert _last(outbox, "send_security_notice")["to"] == user.email


def test_change_password_without_refresh_token_revokes_all(client, db_session, user):
    mine = _login(client)
    r = client.post("/auth/change-password", headers=_auth(mine["access_token"]),
                    json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 204
    assert client.post("/auth/refresh",
                       json={"refresh_token": mine["refresh_token"]}).status_code == 401


def test_change_password_ignores_someone_elses_refresh_token(client, db_session, user):
    other_user = User(email="else@example.com", hashed_password=hash_password(PASSWORD),
                      email_verified=True)
    db_session.add(other_user)
    db_session.flush()
    theirs = _login(client, "else@example.com")["refresh_token"]
    mine = _login(client)
    r = client.post("/auth/change-password", headers=_auth(mine["access_token"]), json={
        "current_password": PASSWORD, "new_password": NEW_PASSWORD, "refresh_token": theirs,
    })
    assert r.status_code == 204
    assert client.post("/auth/refresh",
                       json={"refresh_token": mine["refresh_token"]}).status_code == 401
    # Their token is untouched.
    row = db_session.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_refresh_token(theirs)),
    ).one()
    assert row.revoked_at is None


def test_change_password_wrong_current(client, db_session, user):
    mine = _login(client)
    r = client.post("/auth/change-password", headers=_auth(mine["access_token"]),
                    json={"current_password": "wrong password", "new_password": NEW_PASSWORD})
    assert r.status_code == 400
    db_session.refresh(user)
    assert verify_password(PASSWORD, user.hashed_password)


def test_change_password_rate_limited(client, user):
    mine = _login(client)
    codes = [
        client.post("/auth/change-password", headers=_auth(mine["access_token"]),
                    json={"current_password": "wrong password",
                          "new_password": NEW_PASSWORD}).status_code
        for _ in range(11)
    ]
    assert codes == [400] * 10 + [429]


def test_change_password_requires_auth(client):
    r = client.post("/auth/change-password",
                    json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 401


# ── change email ──────────────────────────────────────────────────────────────

def _request_change(client, access, new_email="moved@example.com", password=PASSWORD):
    return client.post("/auth/change-email/request", headers=_auth(access),
                       json={"new_email": new_email, "current_password": password})


def test_change_email_flow(client, db_session, outbox, user):
    tokens = _login(client)
    trip = Trip(owner_id=user.id, name="t", date=date(2026, 6, 1), region="colorado")
    db_session.add(trip)
    db_session.flush()
    db_session.add(TripMember(trip_id=trip.id, invited_email="Codes@example.com"))
    db_session.flush()

    assert _request_change(client, tokens["access_token"], "Moved@Example.com").status_code == 204
    mail = _last(outbox, "send_email_change_code")
    assert mail["to"] == "moved@example.com"
    db_session.refresh(user)
    assert user.email == "codes@example.com"  # nothing changes until confirm

    r = client.post("/auth/change-email/confirm", headers=_auth(tokens["access_token"]),
                    json={"code": mail["code"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "moved@example.com"
    assert "whumpf_session" in r.cookies
    client.cookies.clear()

    me = client.get("/auth/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == "moved@example.com"
    assert me.json()["email_verified"] is True
    assert client.get("/auth/me", headers=_auth(tokens["access_token"])).status_code == 401
    # Refresh tokens are per user id and keep working.
    assert client.post("/auth/refresh",
                       json={"refresh_token": tokens["refresh_token"]}).status_code == 200

    notice = _last(outbox, "send_security_notice")
    assert notice["to"] == "codes@example.com" and "moved@example.com" in notice["what"]
    invites = db_session.scalars(select(TripMember.invited_email)
                                 .where(TripMember.trip_id == trip.id)).all()
    assert invites == ["moved@example.com"]


def test_change_email_wrong_password(client, outbox, user):
    tokens = _login(client)
    assert _request_change(client, tokens["access_token"], password="wrong one").status_code == 400
    assert outbox == []


def test_change_email_taken_at_request(client, db_session, outbox, user):
    db_session.add(User(email="moved@example.com", hashed_password="x"))
    db_session.flush()
    r = _request_change(client, _login(client)["access_token"])
    assert r.status_code == 400 and r.json()["detail"] == "Email already registered"
    assert outbox == []


def test_change_email_taken_at_confirm(client, db_session, outbox, user):
    tokens = _login(client)
    _request_change(client, tokens["access_token"])
    code = _last(outbox, "send_email_change_code")["code"]
    db_session.add(User(email="moved@example.com", hashed_password="x"))
    db_session.flush()
    r = client.post("/auth/change-email/confirm", headers=_auth(tokens["access_token"]),
                    json={"code": code})
    assert r.status_code == 400 and r.json()["detail"] == "Email already registered"
    db_session.refresh(user)
    assert user.email == "codes@example.com"


def test_change_email_same_or_invalid(client, user):
    access = _login(client)["access_token"]
    assert _request_change(client, access, "codes@example.com").status_code == 400
    assert _request_change(client, access, "not-an-email").status_code == 400


def test_change_email_code_is_per_user(client, db_session, outbox, user):
    other_user = User(email="else@example.com", hashed_password=hash_password(PASSWORD),
                      email_verified=True)
    db_session.add(other_user)
    db_session.flush()
    mine = _login(client)["access_token"]
    theirs = _login(client, "else@example.com")["access_token"]
    _request_change(client, mine)
    code = _last(outbox, "send_email_change_code")["code"]
    r = client.post("/auth/change-email/confirm", headers=_auth(theirs), json={"code": code})
    assert r.status_code == 400
    db_session.refresh(other_user)
    assert other_user.email == "else@example.com"


def test_change_email_wrong_code_burns_after_max_attempts(client, db_session, outbox, user):
    access = _login(client)["access_token"]
    _request_change(client, access)
    code = _last(outbox, "send_email_change_code")["code"]
    for _ in range(MAX_ATTEMPTS):
        r = client.post("/auth/change-email/confirm", headers=_auth(access),
                        json={"code": _wrong(code)})
        assert r.status_code == 400
    r = client.post("/auth/change-email/confirm", headers=_auth(access), json={"code": code})
    assert r.status_code == 400


def test_change_email_requires_auth(client):
    assert client.post("/auth/change-email/request",
                       json={"new_email": "a@b.c", "current_password": PASSWORD}).status_code == 401
    assert client.post("/auth/change-email/confirm", json={"code": "123456"}).status_code == 401


# ── account deletion ──────────────────────────────────────────────────────────

def test_delete_me_removes_email_codes(client, db_session, outbox, user):
    access = _login(client)["access_token"]
    _request_change(client, access)
    user_id = user.id
    r = client.request("DELETE", "/auth/me", headers=_auth(access), json={"password": PASSWORD})
    assert r.status_code == 204
    assert db_session.scalars(select(EmailCode).where(EmailCode.user_id == user_id)).all() == []


def test_delete_me_wrong_password(client, db_session, user):
    access = _login(client)["access_token"]
    r = client.request("DELETE", "/auth/me", headers=_auth(access), json={"password": "wrong one"})
    assert r.status_code == 400 and r.json()["detail"] == "Password is incorrect"
    assert db_session.get(User, user.id) is not None
    assert client.get("/auth/me", headers=_auth(access)).status_code == 200


@pytest.mark.parametrize("kwargs", [{}, {"json": {}}])
def test_delete_me_requires_password_body(client, db_session, user, kwargs):
    access = _login(client)["access_token"]
    r = client.request("DELETE", "/auth/me", headers=_auth(access), **kwargs)
    assert r.status_code == 422
    assert db_session.get(User, user.id) is not None


def test_delete_me_rate_limited(client, user):
    access = _login(client)["access_token"]
    codes = [
        client.request("DELETE", "/auth/me", headers=_auth(access),
                       json={"password": "wrong one"}).status_code
        for _ in range(11)
    ]
    assert codes == [400] * 10 + [429]


def test_delete_me_requires_auth(client):
    r = client.request("DELETE", "/auth/me", json={"password": PASSWORD})
    assert r.status_code == 401
