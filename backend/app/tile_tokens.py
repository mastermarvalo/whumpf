"""Signed tile tokens and tile rate limits.

A tile token is `b64url(json payload) + "." + b64url(HMAC-SHA256)`. It is
deliberately *not* a JWT and is signed with a key derived from JWT_SECRET,
not JWT_SECRET itself: anonymous subjects are chosen by the client, so a
token signed with the session key could be replayed as a session JWT.

Checking a token needs no database lookup. Limits are counted in Redis
(shared across workers) with fixed windows, which keep one small counter per
key instead of one entry per request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import time
from dataclasses import dataclass

from fastapi import HTTPException, Query, Request, status
from limits import RateLimitItem, parse
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter

from app.config import get_settings
from app.rate_limit import client_ip

logger = logging.getLogger("whumpf.tile_access")

TOKEN_TTL_S = 3600
TIER_ANON = "anon"
TIER_USER = "user"
_SUBJECT_PREFIX = {TIER_ANON: "i:", TIER_USER: "u:"}
_MAX_TOKEN_LEN = 512
_KEY_LABEL = b"whumpf-tile-token-v1"

# Bucket → limits, all checked in order. The numbers and the reasoning behind
# them are in docs/PLAN.md (Tile access). Only requests that reach the API
# count; Cloudflare cache hits never get here.
BUCKETS: dict[str, tuple[RateLimitItem, ...]] = {
    # Per client IP. An install renews about once an hour; this covers a
    # household or office behind one IP restarting several devices.
    "token_create": (parse("10/minute"), parse("30/hour")),
    # Per install id. Fast panning with 3–4 layers is ~300–500 tiles/min.
    "anon": (parse("600/minute"), parse("6000/hour")),
    # Per client IP, across all anonymous tokens: caps how far minting fresh
    # install ids can multiply the anon limit (about 2.5 installs at full rate).
    "anon_ip": (parse("1500/minute"), parse("15000/hour")),
    # Per user. Room for 3D terrain, long sessions and offline downloads.
    "user": (parse("1200/minute"), parse("20000/hour")),
    # Per client IP, requests with no token (the old website).
    "tokenless": (parse("180/minute"), parse("2000/hour")),
    # Per client IP, /avalanche/* (public CAIC data, no sign-in). Not tiles,
    # but the same fail-open counting and 429 shape. Cloudflare doesn't cache
    # these, so every map load and zone tap counts.
    "avalanche": (parse("120/minute"), parse("2000/hour")),
    # Per client IP, GET /weather/forecast (spot forecast, needs a tile
    # token). Each tap is one request; Cloudflare doesn't cache it.
    "weather": (parse("60/minute"), parse("600/hour")),
    # Per client IP, GET /cameras and /cameras/{id}/image (needs a tile
    # token). The list once per map load, the image once per tap.
    "cameras": (parse("60/minute"), parse("600/hour")),
    # One global counter ("global"): camera image lookups that reach the
    # Windy API (cache misses). Keeps us from "intensive usage", which
    # Windy's terms forbid, whoever is tapping.
    "windy_upstream": (parse("60/minute"), parse("1000/hour")),
}

_NO_STORE = {"Cache-Control": "no-store"}

_rate_limiter = FixedWindowRateLimiter(
    storage_from_string(get_settings().redis_url or "memory://")
)
_store_error_logged_at = 0.0


@dataclass(frozen=True)
class TileClaims:
    subject: str
    tier: str
    expires_at: int


def _now() -> int:
    return int(time.time())


def _key() -> bytes:
    return hmac.new(get_settings().jwt_secret.encode(), _KEY_LABEL, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict, key: bytes | None = None) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(key or _key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def issue(subject: str, tier: str) -> tuple[str, int]:
    """Return (token, expires_at epoch seconds) for `subject` (e.g. "u:42")."""
    if tier not in _SUBJECT_PREFIX or not subject.startswith(_SUBJECT_PREFIX[tier]):
        raise ValueError(f"subject {subject!r} does not match tier {tier!r}")
    exp = _now() + TOKEN_TTL_S
    return _sign({"v": 1, "sub": subject, "tier": tier, "exp": exp}), exp


def verify(token: str) -> TileClaims | None:
    """Check format, signature, expiry and tier. None on any failure."""
    if len(token) > _MAX_TOKEN_LEN or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    try:
        given = _unb64(sig)
    except (binascii.Error, ValueError):
        return None
    expected = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    sub, tier, exp = payload.get("sub"), payload.get("tier"), payload.get("exp")
    if tier not in _SUBJECT_PREFIX or not isinstance(sub, str) or not isinstance(exp, int):
        return None
    if not sub.startswith(_SUBJECT_PREFIX[tier]) or exp <= _now():
        return None
    return TileClaims(subject=sub, tier=tier, expires_at=exp)


def enforce(bucket: str, subject: str) -> None:
    """Count one request against every limit in `bucket`; 429 if any is over.

    If Redis is unreachable the request is let through (tiles matter more
    than limits during an outage), with a warning at most once a minute.
    """
    global _store_error_logged_at
    for item in BUCKETS[bucket]:
        try:
            allowed = _rate_limiter.hit(item, "tiles", bucket, subject)
            if not allowed:
                reset_at = _rate_limiter.get_window_stats(item, "tiles", bucket, subject).reset_time
        except Exception:
            if time.monotonic() - _store_error_logged_at > 60:
                _store_error_logged_at = time.monotonic()
                logger.warning("tile rate limit store unavailable; allowing requests", exc_info=True)
            return
        if not allowed:
            logger.warning(
                "rate limit hit: bucket=%s subject=%s limit=%s", bucket, subject, item
            )
            retry_after = max(1, math.ceil(reset_at - time.time()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={"Retry-After": str(retry_after), **_NO_STORE},
            )


def limit_token_creation(request: Request) -> None:
    enforce("token_create", client_ip(request))


def require_tile_access(
    request: Request,
    t: str | None = Query(None, description="Tile token from POST /tiles/token"),
) -> None:
    """Router-level dependency for /tiles/*: runs before any rendering."""
    if t is None:
        if not get_settings().tiles_allow_tokenless:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Tile token required",
                headers=_NO_STORE,
            )
        enforce("tokenless", client_ip(request))
        return
    claims = verify(t)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired tile token",
            headers=_NO_STORE,
        )
    if claims.tier == TIER_USER:
        enforce("user", claims.subject)
    else:
        enforce("anon", claims.subject)
        enforce("anon_ip", client_ip(request))
