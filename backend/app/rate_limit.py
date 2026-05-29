"""Rate limiting via slowapi.

Keyed by client IP. The shared `limiter` instance is wired into the FastAPI
app at startup so it can hook the request lifecycle for headers and 429s.

When REDIS_URL is set, state is shared across all uvicorn workers via Redis.
Without it, each worker has its own in-process counter (fine for dev/single-worker).
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

_settings = get_settings()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=_settings.redis_url if _settings.redis_url else None,
)
