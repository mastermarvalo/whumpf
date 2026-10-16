"""SNOTEL station list: new snow over 24 h / 72 h from the daily series."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app.services import snotel

D = date(2026, 10, 15)


def _series(*values: float | None, end: date = D) -> dict[date, float]:
    """Oldest first, ending at `end`; None leaves the day out."""
    n = len(values)
    return {end - timedelta(days=n - 1 - i): v for i, v in enumerate(values) if v is not None}


@pytest.mark.parametrize(
    "values, days, expected",
    [
        ((40, 46), 1, 6.0),             # 6 in overnight
        ((40, 46, 44, 50), 3, 12.0),     # settling between storms isn't subtracted
        ((50, 47), 1, 0.0),              # settled / melted: 0, not negative
        ((40, 40.5), 1, 0.0),            # under the 1 in noise floor
        ((40, 41), 1, 1.0),              # at the floor counts
        ((40, None, 46), 1, None),       # yesterday missing
        ((40, 46, None, 50), 3, None),   # a missing day inside the window
        ((46,), 1, None),                # not enough history
    ],
)
def test_new_snow_depth(values, days, expected):
    assert snotel._new_snow(_series(*values), D, days, snotel._DEPTH_NOISE_IN) == expected


def test_new_swe_floor_survives_float_rounding():
    # 10.3 - 10.2 is 0.0999… in floats; it is still a 0.1 in rise.
    assert snotel._new_snow(_series(10.2, 10.3), D, 1, snotel._SWE_NOISE_IN) == 0.1
    assert snotel._new_snow(_series(10.2, 10.25), D, 1, snotel._SWE_NOISE_IN) == 0.0


def _awdb_element(code: str, rows: dict[date, float | None]) -> dict:
    return {
        "stationElement": {"elementCode": code},
        "values": [
            {"date": d.isoformat(), "value": -9999.0 if v is None else v, "median": 8.0}
            for d, v in sorted(rows.items())
        ],
    }


def test_station_features_carry_new_snow(monkeypatch):
    stations = [
        {"stationTriplet": "1:CO:SNTL", "name": "Snowy", "latitude": 39.6, "longitude": -106.0, "elevation": 11000},
        {"stationTriplet": "2:CO:SNTL", "name": "Gap", "latitude": 39.0, "longitude": -107.0, "elevation": 10000},
        {"stationTriplet": "3:CO:SNTL", "name": "Bare", "latitude": 38.0, "longitude": -107.5, "elevation": 9000},
    ]
    days = [D - timedelta(days=i) for i in range(3, -1, -1)]  # D-3 .. D
    data = {
        "1:CO:SNTL": [
            _awdb_element("SNWD", dict(zip(days, (30, 38, 36, 42), strict=True))),
            _awdb_element("WTEQ", dict(zip(days, (8.0, 8.6, 8.6, 9.1), strict=True))),
        ],
        # D-1 is -9999: 24 h and 72 h unknown, not 0.
        "2:CO:SNTL": [_awdb_element("SNWD", dict(zip(days, (20, 22, None, 25), strict=True)))],
        "3:CO:SNTL": [],
    }

    async def fake_stations():
        return stations

    async def fake_batch(triplets, today, begin):
        return [{"stationTriplet": t, "data": data[t]} for t in triplets]

    monkeypatch.setattr(snotel, "_fetch_stations", fake_stations)
    monkeypatch.setattr(snotel, "_fetch_data_batch", fake_batch)
    monkeypatch.setattr(snotel, "_cache", None)

    fc = asyncio.run(snotel._do_fetch())
    props = {f["properties"]["id"]: f["properties"] for f in fc["features"]}

    snowy = props["1:CO:SNTL"]
    assert snowy["snow_depth_in"] == 42
    assert snowy["new_snow_24h_in"] == 6.0
    assert snowy["new_snow_72h_in"] == 14.0
    assert snowy["new_swe_24h_in"] == 0.5
    assert snowy["new_swe_72h_in"] == 1.1
    assert snowy["new_snow_as_of"] == D.isoformat()

    gap = props["2:CO:SNTL"]
    assert gap["new_snow_24h_in"] is None and gap["new_snow_72h_in"] is None
    assert gap["new_snow_as_of"] == D.isoformat()

    bare = props["3:CO:SNTL"]
    assert {k: bare[k] for k in bare if k.startswith("new_")} == dict.fromkeys(
        ("new_snow_24h_in", "new_snow_72h_in", "new_swe_24h_in", "new_swe_72h_in", "new_snow_as_of")
    )
