"""Signed tile tokens, tile rate limits and client-IP detection."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from limits import parse
from starlette.requests import Request

from app import tile_tokens
from app.auth.service import SESSION_COOKIE, create_access_token, decode_token
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.rate_limit import client_ip
from app.routers import tiles
from tests.test_tiles import _png_bytes

TILE = "/tiles/slope/10/210/394?region=colorado"
INSTALL_ID = "install-0123456789abcdef"
CF_EDGE = "172.64.0.1"  # inside Cloudflare's 172.64.0.0/13


@pytest.fixture
def renders(monkeypatch) -> Iterator[list[str]]:
    """Stub TiTiler and record every render, so tests can assert that a
    rejected request never reached rendering."""
    calls: list[str] = []
    tiles._SLOPE_CACHE.clear()

    async def fake_get(url, params=None, **kwargs):
        calls.append(url)
        return httpx.Response(
            200, content=_png_bytes(512, 512),
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", url, params=params),
        )

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)
    yield calls
    # Don't leave this tile cached for other test modules (test_tiles.py).
    tiles._SLOPE_CACHE.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def small_limits(monkeypatch):
    def _set(bucket: str, *specs: str) -> None:
        monkeypatch.setitem(tile_tokens.BUCKETS, bucket, tuple(parse(s) for s in specs))
    return _set


def _via_cloudflare(ip: str) -> dict[str, str]:
    """X-Forwarded-For as NPM forwards it: client-supplied junk, then the
    client as Cloudflare saw it, then the Cloudflare edge NPM saw."""
    return {"X-Forwarded-For": f"9.9.9.9, {ip}, {CF_EDGE}"}


def _anon_token(install_id: str = INSTALL_ID) -> str:
    return tile_tokens.issue(f"i:{install_id}", tile_tokens.TIER_ANON)[0]


def _split(token: str) -> tuple[dict, str]:
    body, sig = token.split(".")
    return json.loads(tile_tokens._unb64(body)), sig


def _body(payload: dict) -> str:
    return tile_tokens._b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())


# ── the agreed numbers ────────────────────────────────────────────────────────

def test_configured_limits_match_the_plan():
    as_text = {k: [str(i) for i in v] for k, v in tile_tokens.BUCKETS.items()}
    assert as_text == {
        "token_create": ["10 per 1 minute", "30 per 1 hour"],
        "anon": ["600 per 1 minute", "6000 per 1 hour"],
        "anon_ip": ["1500 per 1 minute", "15000 per 1 hour"],
        "user": ["1200 per 1 minute", "20000 per 1 hour"],
        "tokenless": ["180 per 1 minute", "2000 per 1 hour"],
        "avalanche": ["120 per 1 minute", "2000 per 1 hour"],
    }


# ── POST /tiles/token ─────────────────────────────────────────────────────────

def test_anon_token_issued_for_install_id(client):
    resp = client.post("/tiles/token", json={"install_id": INSTALL_ID})
    assert resp.status_code == 200
    claims = tile_tokens.verify(resp.json()["token"])
    assert claims is not None
    assert (claims.subject, claims.tier) == (f"i:{INSTALL_ID}", "anon")
    expires_at = datetime.fromisoformat(resp.json()["expires_at"]).timestamp()
    assert abs(expires_at - (time.time() + 3600)) < 5


def test_user_token_issued_for_bearer(client, db_session, users):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        bearer = create_access_token(users["owner"].email)
        resp = client.post("/tiles/token", headers={"Authorization": f"Bearer {bearer}"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    claims = tile_tokens.verify(resp.json()["token"])
    assert (claims.subject, claims.tier) == (f"u:{users['owner'].id}", "user")


def test_bad_bearer_is_401_not_downgraded_to_anon(client):
    resp = client.post(
        "/tiles/token",
        json={"install_id": INSTALL_ID},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("body", [None, {}, {"install_id": "short"}, {"install_id": "has spaces in it ok?"}])
def test_missing_or_bad_install_id_is_422(client, body):
    resp = client.post("/tiles/token", json=body)
    assert resp.status_code == 422


def test_token_creation_is_limited_per_ip(client, small_limits, caplog):
    small_limits("token_create", "2/minute")
    for i in range(2):
        assert client.post(
            "/tiles/token", json={"install_id": f"{INSTALL_ID}{i}"}, headers=_via_cloudflare("203.0.113.5")
        ).status_code == 200
    with caplog.at_level(logging.WARNING, logger="whumpf.tile_access"):
        resp = client.post(
            "/tiles/token", json={"install_id": f"{INSTALL_ID}x"}, headers=_via_cloudflare("203.0.113.5")
        )
    assert resp.status_code == 429
    assert "bucket=token_create subject=203.0.113.5" in caplog.text
    # A different client is unaffected.
    assert client.post(
        "/tiles/token", json={"install_id": INSTALL_ID}, headers=_via_cloudflare("203.0.113.6")
    ).status_code == 200


# ── token checks on /tiles/* ──────────────────────────────────────────────────

def test_valid_anon_and_user_tokens_render(client, renders):
    user_token = tile_tokens.issue("u:1", tile_tokens.TIER_USER)[0]
    assert client.get(f"{TILE}&t={_anon_token()}").status_code == 200
    assert client.get(f"{TILE}&t={user_token}").status_code == 200


def test_expired_token_is_401_before_rendering(client, renders, monkeypatch):
    real_now = tile_tokens._now()
    monkeypatch.setattr(tile_tokens, "_now", lambda: real_now - 3601)
    token = _anon_token()
    monkeypatch.setattr(tile_tokens, "_now", lambda: real_now)
    resp = client.get(f"{TILE}&t={token}")
    assert resp.status_code == 401
    assert resp.headers["cache-control"] == "no-store"
    assert renders == []


def test_forged_tokens_are_401_before_rendering(client, renders):
    payload, sig = _split(_anon_token())

    # Payload changed (different subject, later expiry) with the old signature.
    tampered = _body({**payload, "sub": "i:someone-else-entirely", "exp": payload["exp"] + 86400})
    # Signed with JWT_SECRET directly instead of the derived tile key.
    raw_key = get_settings().jwt_secret.encode()
    wrong_key = tile_tokens._sign(payload, key=raw_key)
    # A session JWT is not a tile token.
    session_jwt = create_access_token("owner@example.com")

    for token in (f"{tampered}.{sig}", wrong_key, session_jwt, "", "a.b", "garbage"):
        assert client.get(TILE, params={"t": token}).status_code == 401, token
    assert renders == []


def test_tile_token_is_not_a_session_token():
    # Anonymous subjects are client-chosen; a tile token must never decode as
    # a session JWT for that "email".
    token = tile_tokens._sign({"v": 1, "sub": "owner@example.com", "tier": "anon", "exp": tile_tokens._now() + 60})
    assert decode_token(token) is None


def test_wrong_tier_is_401(client, renders):
    payload, sig = _split(_anon_token())
    exp = payload["exp"]
    validly_signed = [
        # Unknown tier.
        tile_tokens._sign({"v": 1, "sub": "u:1", "tier": "admin", "exp": exp}),
        # User tier on an install-id subject, and the reverse.
        tile_tokens._sign({"v": 1, "sub": f"i:{INSTALL_ID}", "tier": "user", "exp": exp}),
        tile_tokens._sign({"v": 1, "sub": "u:1", "tier": "anon", "exp": exp}),
    ]
    # Tier flipped to "user" without re-signing.
    flipped = f"{_body({**payload, 'tier': 'user'})}.{sig}"
    for token in (*validly_signed, flipped):
        assert client.get(TILE, params={"t": token}).status_code == 401
    assert renders == []
    with pytest.raises(ValueError):
        tile_tokens.issue(f"i:{INSTALL_ID}", "user")


def test_missing_token_follows_setting(client, renders, monkeypatch):
    assert client.get(TILE).status_code == 200
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    renders.clear()
    resp = client.get(TILE)
    assert resp.status_code == 401
    assert renders == []


# ── per-bucket limits ─────────────────────────────────────────────────────────

def _hit_until_429(client, url: str, headers=None, n: int = 3) -> list[int]:
    return [client.get(url, headers=headers).status_code for _ in range(n)]


def test_anon_token_limit_per_subject(client, renders, small_limits, caplog):
    small_limits("anon", "2/minute")
    token = _anon_token()
    with caplog.at_level(logging.WARNING, logger="whumpf.tile_access"):
        codes = _hit_until_429(client, f"{TILE}&t={token}")
    assert codes == [200, 200, 429]
    assert f"bucket=anon subject=i:{INSTALL_ID}" in caplog.text
    # A second install on the same IP has its own budget.
    assert client.get(f"{TILE}&t={_anon_token('another-install-0000')}").status_code == 200


def test_429_has_retry_after_and_no_store(client, renders, small_limits):
    small_limits("anon", "1/minute")
    token = _anon_token()
    client.get(f"{TILE}&t={token}")
    resp = client.get(f"{TILE}&t={token}")
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["retry-after"]) <= 60
    assert resp.headers["cache-control"] == "no-store"


def test_user_tier_gets_user_limit_not_anon(client, renders, small_limits):
    small_limits("anon", "1/minute")
    small_limits("user", "3/minute")
    user_token = tile_tokens.issue("u:1", tile_tokens.TIER_USER)[0]
    assert _hit_until_429(client, f"{TILE}&t={user_token}", n=4) == [200, 200, 200, 429]


def test_anon_token_gets_anon_limit_even_with_session(client, renders, small_limits):
    small_limits("anon", "1/minute")
    small_limits("user", "100/minute")
    client.cookies.set(SESSION_COOKIE, create_access_token("owner@example.com"))
    assert _hit_until_429(client, f"{TILE}&t={_anon_token()}", n=2) == [200, 429]


def test_anon_ip_ceiling_caps_many_install_ids(client, renders, small_limits, caplog):
    small_limits("anon_ip", "3/minute")
    headers = _via_cloudflare("203.0.113.7")
    codes = [
        client.get(f"{TILE}&t={_anon_token(f'fresh-install-{i:06d}')}", headers=headers).status_code
        for i in range(3)
    ]
    assert codes == [200, 200, 200]
    with caplog.at_level(logging.WARNING, logger="whumpf.tile_access"):
        resp = client.get(f"{TILE}&t={_anon_token('fresh-install-999999')}", headers=headers)
    assert resp.status_code == 429
    assert "bucket=anon_ip subject=203.0.113.7" in caplog.text
    # The ceiling is per IP and doesn't apply to signed-in users.
    user_token = tile_tokens.issue("u:1", tile_tokens.TIER_USER)[0]
    assert client.get(f"{TILE}&t={user_token}", headers=headers).status_code == 200
    assert client.get(
        f"{TILE}&t={_anon_token('fresh-install-999999')}", headers=_via_cloudflare("203.0.113.8")
    ).status_code == 200


def test_tokenless_limit_per_real_ip_ignores_spoofed_xff(client, renders, small_limits, caplog):
    small_limits("tokenless", "2/minute")
    # Each request claims a different leftmost IP; all share one real client.
    codes = [
        client.get(TILE, headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9, {CF_EDGE}"}).status_code
        for i in range(2)
    ]
    assert codes == [200, 200]
    with caplog.at_level(logging.WARNING, logger="whumpf.tile_access"):
        resp = client.get(TILE, headers={"X-Forwarded-For": f"10.0.0.99, 203.0.113.9, {CF_EDGE}"})
    assert resp.status_code == 429
    assert "bucket=tokenless subject=203.0.113.9" in caplog.text
    assert client.get(TILE, headers=_via_cloudflare("203.0.113.10")).status_code == 200


def test_limiter_store_down_lets_tiles_through(client, renders, monkeypatch):
    class Broken:
        def hit(self, *a, **kw):
            raise ConnectionError("redis down")

    monkeypatch.setattr(tile_tokens, "_rate_limiter", Broken())
    assert client.get(f"{TILE}&t={_anon_token()}").status_code == 200


# ── client IP ─────────────────────────────────────────────────────────────────

def _request(xff: str | None, peer: str = "192.168.3.123") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 4321)})


@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        # Through Cloudflare: the hop before the edge, whatever the client prepended.
        (f"203.0.113.9, {CF_EDGE}", "203.0.113.9"),
        (f"1.1.1.1, 6.6.6.6, 203.0.113.9, {CF_EDGE}", "203.0.113.9"),
        ("2001:db8::1, 2606:4700::1", "2001:db8::1"),
        # Straight to NPM (not a Cloudflare edge): the last hop is the client.
        ("1.2.3.4, 198.51.100.7", "198.51.100.7"),
        # NPM later sending only CF-Connecting-IP.
        ("203.0.113.9", "203.0.113.9"),
        # No usable header: the socket peer.
        (None, "192.168.3.123"),
        ("not-an-ip", "192.168.3.123"),
    ],
)
def test_client_ip(xff, expected):
    assert client_ip(_request(xff)) == expected
