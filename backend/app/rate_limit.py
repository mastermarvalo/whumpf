"""Rate limiting via slowapi.

Keyed by client IP. The shared `limiter` instance is wired into the FastAPI
app at startup so it can hook the request lifecycle for headers and 429s.

Single-process in-memory limiter is fine for a single uvicorn worker; for
multi-worker / multi-instance deploys, swap to Redis backend (slowapi supports
this via the `storage_uri` arg).
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# Trust the immediate peer's IP by default. When deployed behind a reverse
# proxy (nginx, ALB), set `--forwarded-allow-ips=*` on uvicorn and slowapi
# will pick up X-Forwarded-For via request.client automatically.
limiter = Limiter(key_func=get_remote_address, default_limits=[])
