"""Database engine and session management.

Single global engine, one session per request (via the ``get_session``
dependency). We use SQLAlchemy 2.x style throughout.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
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
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_s,
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


def open_session() -> Session:
    """A session for a long-running job outside any request (the pack
    worker). The caller closes it."""
    return _sessionmaker()()


@contextmanager
def job_session() -> Iterator[Session]:
    """``with job_session() as session:`` for work outside a request: a CLI
    job (make banner, strava-refresh) or a webhook's background task, which
    runs after the request's own session is closed. Closed on exit."""
    session = open_session()
    try:
        yield session
    finally:
        session.close()
