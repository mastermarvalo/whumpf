"""Admin-check helpers.

Two paths to admin status, either suffices:
  1. `users.is_admin = true` on the user's row. Set via SQL or future UI.
  2. The user's email appears in the ADMIN_EMAILS env CSV.

The env allowlist is the bootstrap path — set it once, log in, and you're
admin without ever needing to touch the DB. The column is the long-term
path so you can grant admin to other users later without restarting the api.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, HTTPException, status

from app.auth.dependencies import get_current_user
from app.config import get_settings
from app.models.user import User


@lru_cache(maxsize=1)
def _admin_email_set() -> frozenset[str]:
    raw = get_settings().admin_emails
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_admin(user: User) -> bool:
    if user.is_admin:
        return True
    return user.email.lower() in _admin_email_set()


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
