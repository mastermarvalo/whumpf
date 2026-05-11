"""Symmetric encryption for sensitive DB columns (currently Strava OAuth tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256) from cryptography, already a transitive
dependency via python-jose[cryptography]. Fernet output is base64url-encoded
and fits in a varchar — encrypting a 40-char Strava token produces ~140 chars
of ciphertext, well under the existing 255 column.

Backwards-compat read: rows written before encryption was enabled return as-is
when Fernet rejects them as invalid tokens. Those rows transparently get
re-encrypted on the next write (typical: every hour when Strava tokens refresh).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().strava_token_key
    if not key:
        raise RuntimeError(
            "STRAVA_TOKEN_KEY is not set. Generate with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


class EncryptedString(TypeDecorator):
    """String column transparently encrypted at rest via Fernet."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        return _fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # Legacy plaintext from before encryption was enabled. Will be
            # re-encrypted on next write (e.g., on Strava token refresh).
            return value
