"""Shared test fixtures.

Tests never touch the real database. At session start the ``whumpf_test``
database (same server as DATABASE_URL) is dropped, recreated empty and built
with ``alembic upgrade head`` — so every run proves the migrations produce a
working schema. Each test then runs inside an outer transaction that is rolled
back at the end, with the app's own ``commit()`` calls landing on savepoints.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from sqlalchemy.engine import URL, make_url

# The only database these fixtures will ever drop or create.
TEST_DATABASE = "whumpf_test"

# Point the app at the test database before anything imports app settings
# (get_settings is cached). Only the database name changes.
os.environ["DATABASE_URL"] = (
    make_url(os.environ["DATABASE_URL"]).set(database=TEST_DATABASE).render_as_string(hide_password=False)
)
# Never send real email from tests: the container env may select Resend.
os.environ["MAIL_PROVIDER"] = "console"
os.environ["RESEND_API_KEY"] = ""

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import User

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def require_test_database(url: URL) -> None:
    """Hard stop before any DROP/CREATE: the target must be exactly whumpf_test."""
    if url.database != TEST_DATABASE:
        raise RuntimeError(
            f"Refusing to drop/create database {url.database!r}; tests only "
            f"ever recreate {TEST_DATABASE!r}."
        )


def alembic_config(url: URL) -> Config:
    require_test_database(url)
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = url.render_as_string(hide_password=False)
    cfg.attributes["skip_logging"] = True  # keep pytest's log capture intact
    return cfg


def recreate_test_database(url: URL) -> None:
    require_test_database(url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEST_DATABASE}"')
    finally:
        admin.dispose()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that opens a network connection beyond loopback or a
    unix socket. The attempt raises OSError (as if offline, so app code takes
    its normal error path) and the test fails at teardown even if the app
    swallowed it. libpq (the test DB) uses C sockets and isn't affected."""
    import ipaddress
    import socket

    attempts: list[str] = []

    def _local(host: str) -> bool:
        if host in ("localhost", "testserver"):
            return True
        try:
            return ipaddress.ip_address(host.split("%")[0]).is_loopback
        except ValueError:
            return False

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if host is not None and not _local(name):
            attempts.append(f"resolve {name}")
            raise socket.gaierror(socket.EAI_NONAME, f"network disabled in tests: {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _guard(connect):
        def guarded(self, address):
            if self.family != socket.AF_UNIX and not _local(str(address[0])):
                attempts.append(f"connect {address[0]}")
                raise OSError(f"network disabled in tests: {address[0]}")
            return connect(self, address)
        return guarded

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(socket.socket.connect_ex))
    yield
    if attempts:
        pytest.fail(f"test tried to use the network: {attempts}")


@pytest.fixture(autouse=True)
def tile_rate_limits(monkeypatch):
    """Fresh in-memory tile limit counters per test, so tests never touch the
    real Redis counters or leak hits into each other."""
    from limits.storage import MemoryStorage
    from limits.strategies import FixedWindowRateLimiter

    from app import tile_tokens

    monkeypatch.setattr(tile_tokens, "_rate_limiter", FixedWindowRateLimiter(MemoryStorage()))


@pytest.fixture(autouse=True)
def endpoint_rate_limits(monkeypatch):
    """Same for the slowapi per-endpoint limits (@limiter.limit on /auth/*)
    and the per-email/per-user limits (enforce_key_limits)."""
    from limits.storage import MemoryStorage
    from limits.strategies import FixedWindowRateLimiter

    from app import rate_limit
    from app.rate_limit import limiter

    storage = MemoryStorage()
    monkeypatch.setattr(limiter, "_storage", storage)
    monkeypatch.setattr(limiter, "_limiter", type(limiter._limiter)(storage))
    monkeypatch.setattr(rate_limit, "_key_limiter", FixedWindowRateLimiter(MemoryStorage()))


@pytest.fixture(autouse=True)
def status_cache(monkeypatch):
    """Each test starts with no cached /status service checks."""
    from app.routers import status

    monkeypatch.setattr(status, "_services", None)


@pytest.fixture(scope="session")
def test_db_url() -> URL:
    url = make_url(get_settings().database_url)
    require_test_database(url)
    return url


@pytest.fixture(scope="session")
def engine(test_db_url: URL):
    recreate_test_database(test_db_url)
    command.upgrade(alembic_config(test_db_url), "head")
    eng = create_engine(test_db_url, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine) -> Iterator[Session]:
    conn = engine.connect()
    trans = conn.begin()
    # create_savepoint → the app's commit()s land on a nested savepoint, so the
    # outer rollback below cleanly undoes everything the test wrote.
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def users(db_session: Session) -> dict[str, User]:
    owner = User(email="owner@example.com", hashed_password="x")
    other = User(email="other@example.com", hashed_password="x")
    third = User(email="third@example.com", hashed_password="x")
    db_session.add_all([owner, other, third])
    db_session.flush()
    return {"owner": owner, "other": other, "third": third}


@pytest.fixture
def make_client(db_session: Session, users: dict[str, User]) -> Iterator[Callable]:
    """Returns a factory: ``make_client("owner")`` → TestClient authed as that user."""

    def _make(user_key: str = "owner") -> TestClient:
        app.dependency_overrides[get_session] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: users[user_key]
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()
