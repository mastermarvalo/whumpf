"""Database engine and session management.

Single global engine, one session per request (via the ``get_session``
dependency). We use SQLAlchemy 2.x style throughout.
"""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine.

    ``pool_pre_ping`` guards against stale connections after network blips
    (common when Postgres is restarted but the API isn't).
    """
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


@lru_cache
def _sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a session, closes it when done."""
    session = _sessionmaker()()
    try:
        yield session
    finally:
        session.close()
