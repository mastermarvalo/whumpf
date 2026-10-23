"""GET /legal/privacy and /legal/terms: public pages, rendered from
app/legal/*.md, with the contact email, cache headers, no scripts and the
per-IP `legal` limit. Also the access-log redaction the privacy policy
relies on (no tile token, OAuth state or map point in uvicorn's lines)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import log_redact
from app.main import app
from app.routers import legal

CONTACT = "privacy@example.org"

PRIVACY_SECTIONS = [
    "<h1>Whumpf Privacy Policy</h1>",
    "Last updated:",
    "What our server stores, and for how long",
    "Server logs and rate limits",
    "Data we send to others on your behalf",
    "Services that handle data for us",
    "<h2>Backups</h2>",
    "<h2>Strava</h2>",
    "Strava may monitor and collect usage data",
    "artificial intelligence",
    "within 30 days",
    "<h2>Deleting your account</h2>",
    "<h2>Your rights</h2>",
    "GDPR",
    "<h2>Children</h2>",
    "not directed at children",
    '<div class="table"><table>',
]

TERMS_SECTIONS = [
    "<h1>Whumpf Terms of Use</h1>",
    "Last updated:",
    "Whumpf is a beta",
    "It is not a substitute for avalanche training, the CAIC forecast, or your own judgement in the field.",
    "Use Whumpf at your own risk.",
    "<h2>Acceptable use</h2>",
    "Data from other organizations",
    "<h2>Strava</h2>",
    "Ending your account",
    "No warranty",
    "Changes to these terms",
    "<h2>Governing law</h2>",
    "<li>scrape, bulk-download",
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(legal.get_settings(), "legal_contact_email", CONTACT)
    legal.render.cache_clear()
    yield TestClient(app)
    legal.render.cache_clear()


@pytest.mark.parametrize(("path", "sections"), [
    ("/legal/privacy", PRIVACY_SECTIONS),
    ("/legal/terms", TERMS_SECTIONS),
])
def test_page_renders_with_key_sections(client, path, sections):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    for section in sections:
        assert section in html, section
    assert f'<a href="mailto:{CONTACT}">{CONTACT}</a>' in html
    assert "{contact_email}" not in html
    assert "<script" not in html.lower()
    assert 'name="viewport"' in html
    assert "prefers-color-scheme: dark" in html


def test_cacheable_and_locked_down(client):
    resp = client.get("/legal/privacy")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_head(client):
    resp = client.head("/legal/terms")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=3600"


def test_no_contact_configured(monkeypatch):
    monkeypatch.setattr(legal.get_settings(), "legal_contact_email", "")
    legal.render.cache_clear()
    try:
        html = TestClient(app).get("/legal/privacy").text
    finally:
        legal.render.cache_clear()
    assert "(contact email not configured)" in html
    assert "mailto:" not in html


def test_rate_limited_per_ip(client):
    for _ in range(60):
        assert client.get("/legal/privacy").status_code == 200
    resp = client.get("/legal/terms")
    assert resp.status_code == 429
    assert resp.headers["cache-control"] == "no-store"


def test_unknown_page_404(client):
    assert client.get("/legal/cookies").status_code == 404


def _access_line(path: str) -> str:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.5:1234", "GET", path, "1.1", 200), None,
    )
    log_redact.AccessRedactFilter().filter(record)
    return record.getMessage()


def test_access_log_drops_token_and_points():
    line = _access_line("/weather/forecast?lat=39.7984&lon=-105.7764&t=eyJzdWIiOiJ1OjQyIn0.c2ln")
    assert "39.7984" not in line and "105.7764" not in line and "eyJ" not in line
    assert "/weather/forecast?lat=<redacted>&lon=<redacted>&t=<redacted>" in line

    line = _access_line("/avalanche/zone_detail?lat=39.1&lng=-106.2")
    assert "39.1" not in line and "106.2" not in line

    line = _access_line("/tiles/slope/10/210/394?region=colorado&t=abc.def")
    assert line.count("<redacted>") == 1
    assert "/tiles/slope/10/210/394?region=colorado&t=<redacted>" in line

    line = _access_line("/auth/strava/callback?code=c0de&state=eyJ1aWQiOjQyfQ&scope=read")
    assert "c0de" not in line and "eyJ" not in line
    assert "scope=read" in line


def test_access_log_keeps_other_params():
    # Only whole parameter names match: format=, start=, at= are untouched.
    line = _access_line("/x?format=png&start=2&at=5&region=colorado")
    assert "<redacted>" not in line
