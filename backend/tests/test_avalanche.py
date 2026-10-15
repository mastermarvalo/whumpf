"""/avalanche/observations: fetched_at marks when the cache was filled from CAIC."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from app import http_retry
from app.routers import avalanche


class _Resp:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> list:
        return [{"report_id": "a1", "latitude": 39.6, "longitude": -106.0}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(avalanche, "_obs_cache", None)
    monkeypatch.delitem(http_retry._BREAKERS, "caic-obs", raising=False)
    yield
    http_retry._BREAKERS.pop("caic-obs", None)


def test_fetched_at_set_and_kept_when_refresh_fails(monkeypatch):
    async def ok(*a, **k):
        return _Resp()

    monkeypatch.setattr(avalanche._HTTP, "get", ok)
    first = asyncio.run(avalanche.get_observations())
    stamp = datetime.fromisoformat(first["fetched_at"])
    assert stamp.utcoffset() == timedelta(0)
    assert len(first["features"]) == 1

    # Expire the cache, then make CAIC fail: stale data must keep its stamp.
    data, fetched = avalanche._obs_cache
    monkeypatch.setattr(avalanche, "_obs_cache", (data, fetched - timedelta(hours=1)))

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    async def run_stale():
        monkeypatch.setattr(avalanche._HTTP, "get", boom)
        monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: __import__("tenacity").wait_none())
        served = await avalanche.get_observations()
        await avalanche._bg_refresh_obs()
        return served, await avalanche.get_observations()

    served, after = asyncio.run(run_stale())
    assert served["fetched_at"] == first["fetched_at"]
    assert after["fetched_at"] == first["fetched_at"]
