from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Admin flag — independent of ADMIN_EMAILS env allowlist; either grants
    # admin. See app.admin.is_admin().
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # CSV of region IDs this user can access (e.g. "colorado,utah"). Validated
    # against the REGIONS registry by the /regions endpoint and any future
    # per-region feature gates.
    allowed_regions: Mapped[str] = mapped_column(
        String(255), default="colorado", server_default="colorado", nullable=False,
    )

    # Email verification — login requires it unless SKIP_EMAIL_VERIFICATION.
    # The *_token columns are the legacy link flow (old website): SHA-256 of
    # the link token, one outstanding per user. Codes live in email_codes.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    email_verification_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    email_verification_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Password reset — same pattern. Link tokens are short-lived (1h) and
    # consumed on first successful reset.
    password_reset_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    password_reset_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    strava_connection = relationship("StravaConnection", back_populates="user", uselist=False)

    @property
    def region_ids(self) -> list[str]:
        return [r.strip() for r in self.allowed_regions.split(",") if r.strip()]
