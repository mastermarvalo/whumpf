"""Shared test fixtures.

Runs against the PostGIS database the container is wired to (``make test-api``
execs pytest inside the api container). Each test runs inside an outer
transaction that is rolled back at the end, with the app's own ``commit()``
calls landing on savepoints — so tests are isolated and leave no rows behind.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Base, User


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(get_settings().database_url, future=True)
    Base.metadata.create_all(eng)
    return eng


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
