"""/docs and /openapi.json are served in dev and absent when WHUMPF_ENV=prod."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import get_settings


def _client(monkeypatch, env: str) -> TestClient:
    settings = get_settings().model_copy(update={"whumpf_env": env})
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return TestClient(main.create_app())


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_docs_and_schema_hidden_in_prod(monkeypatch, path):
    assert _client(monkeypatch, "prod").get(path).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_and_schema_served_in_dev(monkeypatch, path):
    assert _client(monkeypatch, "dev").get(path).status_code == 200


def test_redoc_never_served(monkeypatch):
    assert _client(monkeypatch, "dev").get("/redoc").status_code == 404
