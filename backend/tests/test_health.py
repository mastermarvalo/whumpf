"""Smoke test for /healthz — runs without a DB.

Does not cover /readyz since that requires Postgres. Readiness is better
tested end-to-end against the running stack.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
