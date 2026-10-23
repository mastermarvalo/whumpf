"""Migrations: the models and migrations agree, the baseline round-trips, and
the guards that keep a stray command away from real data hold."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from app.main import app
from tests.conftest import alembic_config, recreate_test_database, require_test_database

_BASELINE = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0001_baseline.py"


@pytest.fixture(scope="module")
def baseline():
    spec = importlib.util.spec_from_file_location("baseline_0001", _BASELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_models_match_migrations(engine, test_db_url):
    """Fails when a model changed without a migration (`make makemigration`)."""
    command.check(alembic_config(test_db_url))


def test_baseline_downgrade_then_upgrade(engine, test_db_url):
    cfg = alembic_config(test_db_url)
    command.downgrade(cfg, "base")
    assert "users" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    assert "users" in inspect(engine).get_table_names()
    command.check(cfg)


def test_0003_backfills_queued_at_from_created_at(engine, test_db_url):
    cfg = alembic_config(test_db_url)
    command.downgrade(cfg, "0002_packs")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO packs (id, region, bbox, layers, created_at, last_requested_at)"
                " VALUES ('4f08336c-0000-4000-8000-000000000000', 'colorado',"
                " ST_GeomFromText('POLYGON((-105.8 39.7,-105.7 39.7,-105.7 39.8,-105.8 39.8,-105.8 39.7))', 4326),"
                " '{topo}', '2026-10-19 12:00+00', '2026-10-19 12:00+00')")
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.exec_driver_sql("SELECT queued_at = created_at, tile_bytes::text FROM packs").one()
        assert tuple(row) == (True, "{}")
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM packs")


def test_baseline_downgrade_refuses_other_databases(baseline, monkeypatch):
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_DOWNGRADE", raising=False)
    for name in ("whumpf", "whumpf_test2", "postgres", "WHUMPF_TEST"):
        with pytest.raises(RuntimeError, match="Refusing to downgrade"):
            baseline.check_downgrade_allowed(name)
    baseline.check_downgrade_allowed("whumpf_test")

    monkeypatch.setenv("ALLOW_DESTRUCTIVE_DOWNGRADE", "true")  # must be exactly "1"
    with pytest.raises(RuntimeError):
        baseline.check_downgrade_allowed("whumpf")
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_DOWNGRADE", "1")
    baseline.check_downgrade_allowed("whumpf")


def test_baseline_upgrade_refuses_unstamped_existing_schema(baseline):
    with pytest.raises(RuntimeError, match="stamp"):
        baseline.check_upgrade_allowed({"users", "routes", "spatial_ref_sys"})
    baseline.check_upgrade_allowed({"spatial_ref_sys"})  # fresh PostGIS database


def test_fixtures_refuse_any_database_but_whumpf_test():
    for name in ("whumpf", "postgres", "whumpf_test_old"):
        url = make_url(f"postgresql+psycopg://u:p@localhost:5432/{name}")
        with pytest.raises(RuntimeError, match="Refusing"):
            require_test_database(url)
        with pytest.raises(RuntimeError, match="Refusing"):
            recreate_test_database(url)
        with pytest.raises(RuntimeError, match="Refusing"):
            alembic_config(url)


def test_readyz_reports_schema_at_head(engine, test_db_url):
    head = ScriptDirectory.from_config(alembic_config(test_db_url)).get_heads()
    body = TestClient(app).get("/readyz").json()
    assert body["checks"]["schema"]["ok"] is True
    assert body["checks"]["schema"]["revision"] == head == ["0004_strava_fetched_at"]
