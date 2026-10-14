"""Rate limiting via slowapi.

Keyed by client IP (see `client_ip`). The shared `limiter` instance is wired
into the FastAPI app at startup so it can hook the request lifecycle for
headers and 429s.

When REDIS_URL is set, state is shared across all uvicorn workers via Redis.
Without it, each worker has its own in-process counter (fine for dev/single-worker).
"""

from __future__ import annotations

import ipaddress

from slowapi import Limiter
from starlette.requests import Request

from app.config import get_settings

_settings = get_settings()

# Cloudflare edge ranges, from https://www.cloudflare.com/ips/ (checked
# 2026-10-13). They change rarely; update this list if Cloudflare adds one.
_CLOUDFLARE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
        "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
        "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
        "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
        "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
        "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
    )
)


def _parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(raw.strip())
    except ValueError:
        return None


def _is_cloudflare(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _CLOUDFLARE_NETS)


def client_ip(request: Request) -> str:
    """The real client address, for rate-limit keys.

    Path: client → Cloudflare → Nginx Proxy Manager → API. Each proxy appends
    to X-Forwarded-For, so only the right-hand end is trustworthy; whatever
    the client sent itself sits on the left. The last entry is the address NPM
    saw. If that is a Cloudflare edge, the entry before it is the client as
    Cloudflare saw it; otherwise someone reached NPM directly and the last
    entry *is* the client. This stays correct if NPM is later switched to
    send a single CF-Connecting-IP value.

    Reads the raw header because uvicorn runs with --forwarded-allow-ips='*'
    in prod, which rewrites request.client from the spoofable leftmost entry.
    Relies on ufw letting only NPM reach :8000.
    """
    peer = request.client.host if request.client else "unknown"
    header = request.headers.get("x-forwarded-for")
    if not header:
        return peer
    hops = [h for h in header.split(",") if h.strip()]
    last = _parse_ip(hops[-1]) if hops else None
    if last is None:
        return peer
    if _is_cloudflare(last) and len(hops) >= 2:
        before = _parse_ip(hops[-2])
        if before is not None:
            return str(before)
    return str(last)


limiter = Limiter(
    key_func=client_ip,
    default_limits=[],
    storage_uri=_settings.redis_url if _settings.redis_url else None,
)
