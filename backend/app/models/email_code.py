from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class CodePurpose(str, enum.Enum):
    verify_email = "verify_email"
    password_reset = "password_reset"
    change_email = "change_email"


class EmailCode(Base):
    """A 6-digit code mailed to ``email``. Only an HMAC of the code is stored.

    At most one live row per (user, purpose): issuing a new code deletes the
    old one. See ``app.auth.codes``.
    """

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    purpose: Mapped[CodePurpose] = mapped_column(
        Enum(CodePurpose, native_enum=False, length=20), nullable=False,
    )
    # Where the code was sent. For change_email this is the new address.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
