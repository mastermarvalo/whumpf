"""6-digit email codes (verify email, reset password, change email) and the
hashed legacy link tokens they replace.

A 6-digit code has only 10^6 values, so a plain hash could be reversed from a
DB dump in seconds. Codes are HMACed with a key derived from JWT_SECRET
instead, bound to the user and purpose. Guessing is capped at
MAX_ATTEMPTS per code; per-email and per-IP request limits live in the router.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.email_code import CodePurpose, EmailCode

CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5


class CodeError(Exception):
    """Code unknown, wrong, expired, consumed or out of attempts."""


def _key() -> bytes:
    return hmac.new(get_settings().jwt_secret.encode(), b"whumpf-email-code", "sha256").digest()


def _hash_code(user_id: int, purpose: CodePurpose, code: str) -> str:
    msg = f"{user_id}:{purpose.value}:{code}".encode()
    return hmac.new(_key(), msg, "sha256").hexdigest()


def issue_code(session: Session, user_id: int, purpose: CodePurpose, email: str) -> str:
    """Replace any code ``user_id`` holds for ``purpose``; return the plaintext.
    Caller commits."""
    discard_codes(session, user_id, purpose)
    code = f"{secrets.randbelow(10**6):06d}"
    session.add(EmailCode(
        user_id=user_id,
        purpose=purpose,
        email=email,
        code_hash=_hash_code(user_id, purpose, code),
        expires_at=datetime.now(timezone.utc) + CODE_TTL,
    ))
    return code


def check_code(session: Session, user_id: int, purpose: CodePurpose, code: str) -> EmailCode:
    """Consume and return the user's live code for ``purpose`` if ``code``
    matches. A wrong guess uses up an attempt; the MAX_ATTEMPTS-th burns the
    code. Raises CodeError. Caller commits — also on error, so attempts stick.
    """
    now = datetime.now(timezone.utc)
    # FOR UPDATE: parallel guesses serialize, so none slip past MAX_ATTEMPTS.
    row = session.scalars(
        select(EmailCode)
        .where(
            EmailCode.user_id == user_id,
            EmailCode.purpose == purpose,
            EmailCode.consumed_at.is_(None),
        )
        .with_for_update(),
    ).first()
    if row is None or row.expires_at <= now or row.attempts >= MAX_ATTEMPTS:
        raise CodeError("Invalid or expired code")
    if not hmac.compare_digest(row.code_hash, _hash_code(user_id, purpose, code.strip())):
        row.attempts += 1
        if row.attempts >= MAX_ATTEMPTS:
            row.consumed_at = now
        raise CodeError("Invalid or expired code")
    row.consumed_at = now
    return row


def discard_codes(session: Session, user_id: int, purpose: CodePurpose | None = None) -> None:
    """Delete the user's codes (for one purpose, or all). Caller commits."""
    stmt = delete(EmailCode).where(EmailCode.user_id == user_id)
    if purpose is not None:
        stmt = stmt.where(EmailCode.purpose == purpose)
    session.execute(stmt)


# ── legacy link tokens (old website; remove with LEGACY_EMAIL_LINKS) ──────────

def new_link_token() -> tuple[str, str]:
    """(plaintext for the email link, SHA-256 to store on the user row)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_link_token(raw)


def hash_link_token(raw: str) -> str:
    # Plain SHA-256 is enough: the token is 256 random bits.
    return hashlib.sha256(raw.encode()).hexdigest()
