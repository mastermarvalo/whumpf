"""Offline packs (app/services/packs.py, app/jobs/pack_worker.py): tile lists,
PMTiles written and read back tile for tile, MD5 and bytes of what goes to
R2, failures, the queue, and account deletion forgetting the requester."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from fastapi import HTTPException
from pmtiles.reader import MemorySource, Reader
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from sqlalchemy import select

from app.auth.service import hash_password
from app.config import get_settings
from app.models import Pack
from app.services import packs
from app.services.pack_cache import LAYERS, PackTileError
from app.services.packs import (
    PackBuildError,
    R2Store,
    TileRange,
    build_pack,
    check_request,
    claim_next,
    grid_range,
    layer_tiles,
    queue_pack,
    requeue_interrupted,
)

# About 10 x 10 km at Berthoud Pass.
BERTHOUD = (-105.835, 39.753, -105.719, 39.843)
SMALL = (-105.78, 39.79, -105.77, 39.80)  # ~1 km


# ── tile lists ─────────────────────────────────────────────────────────────────

def test_box_on_tile_edges_takes_exactly_those_tiles():
    tiles = TileRange(14, 3376, 6200, 3378, 6202)
    assert grid_range(tiles.bounds()) == tiles


def test_box_just_past_an_edge_takes_the_next_tile():
    tiles = TileRange(14, 3376, 6200, 3378, 6202)
    west, south, east, north = tiles.bounds()
    assert grid_range((west, south, east + 1e-6, north)) == TileRange(14, 3376, 6200, 3379, 6202)
    assert grid_range((west - 1e-6, south, east, north)) == TileRange(14, 3375, 6200, 3378, 6202)
    assert grid_range((west, south - 1e-6, east, north)).y1 == 6203


def test_box_rounds_out_to_zoom_14():
    base = grid_range(BERTHOUD)
    assert base.z == 14
    west, south, east, north = base.bounds()
    assert west <= BERTHOUD[0] and south <= BERTHOUD[1] and east >= BERTHOUD[2] and north >= BERTHOUD[3]
    # never more than one tile (~1.9 km at zoom 14 here) beyond the box
    tile_deg = 360 / (1 << 14)
    assert BERTHOUD[0] - west < tile_deg and east - BERTHOUD[2] < tile_deg


def test_zoom_ranges_per_layer():
    base = grid_range(BERTHOUD)
    for name, spec in LAYERS.items():
        zooms = {z for z, _, _ in layer_tiles(name, base)}
        assert zooms == set(range(spec.minzoom, spec.maxzoom + 1)), name
    topo = layer_tiles("topo", base)
    by_zoom = Counter(z for z, _, _ in topo)
    assert by_zoom[14] == base.count()
    assert by_zoom[16] == 16 * base.count()  # children: exactly the same area
    assert by_zoom[8] == 1


def test_lower_zooms_are_the_parents():
    base = TileRange(14, 3376, 6200, 3381, 6203)
    assert base.at(12) == TileRange(12, 844, 1550, 845, 1550)
    assert base.at(16) == TileRange(16, 3376 * 4, 6200 * 4, 3382 * 4 - 1, 6204 * 4 - 1)


def test_elevation_is_padded_by_two_zoom_14_tiles():
    base = grid_range(BERTHOUD)
    elevation = layer_tiles("elevation", base)
    z14 = {(x, y) for z, x, y in elevation if z == 14}
    assert min(x for x, _ in z14) == base.x0 - 2 and max(x for x, _ in z14) == base.x1 + 2
    assert min(y for _, y in z14) == base.y0 - 2 and max(y for _, y in z14) == base.y1 + 2
    assert len(z14) == (base.x1 - base.x0 + 5) * (base.y1 - base.y0 + 5)
    # lower zooms cover the padded area too
    padded = base.pad(2)
    z12 = {(x, y) for z, x, y in elevation if z == 12}
    assert z12 == {(x, y) for _, x, y in padded.at(12).tiles()}
    # and slope isn't padded
    assert {(x, y) for z, x, y in layer_tiles("slope", base) if z == 14} == {
        (x, y) for _, x, y in base.tiles()}


def test_padding_stops_at_the_edge_of_the_grid():
    assert TileRange(14, 0, 1, 1, 2).pad(2) == TileRange(14, 0, 0, 3, 4)
    last = (1 << 14) - 1
    assert TileRange(14, last, last, last, last).pad(2) == TileRange(14, last - 2, last - 2, last, last)


def test_tiles_are_in_tile_id_order_without_repeats():
    tiles = layer_tiles("imagery", grid_range(BERTHOUD))
    ids = [zxy_to_tileid(*t) for t in tiles]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


# ── requests ───────────────────────────────────────────────────────────────────

def test_request_defaults_to_every_layer_in_order():
    box_, layers = check_request("colorado", BERTHOUD, None)
    assert box_ == BERTHOUD and layers == list(LAYERS)
    assert check_request("colorado", BERTHOUD, ["ski", "topo"])[1] == ["topo", "ski"]


@pytest.mark.parametrize("bbox, layers, message", [
    (BERTHOUD, ["topo", "nope"], "unknown: nope"),
    (BERTHOUD, [], "layers must be"),
    ((-105.7, 39.75, -105.8, 39.85), None, "west < east"),
    ((-106.0, 39.7, -105.6, 39.9), None, r"km²\); the most is 700 km²"),
    ((-106.5, 39.70, -105.8, 39.72), None, "the most is 50 km on a side"),
    ((-104.4, 39.7, -104.3, 39.8), None, "inside the colorado region"),
    ((-105.8, 39.7, float("nan"), 39.8), None, "four numbers"),
])
def test_bad_requests_are_refused(bbox, layers, message):
    with pytest.raises(ValueError, match=message):
        check_request("colorado", bbox, layers)


def test_unknown_region_is_refused():
    with pytest.raises(HTTPException):
        check_request("narnia", BERTHOUD, None)


# ── fakes ──────────────────────────────────────────────────────────────────────

def _tile_bytes(layer: str, z: int, x: int, y: int) -> bytes:
    if layer == "ski":
        return b"\x1f\x8b" + f"{z}/{x}/{y}".encode() if (x + y) % 3 == 0 else b""
    # Even rows repeat one tile per zoom, so there are identical tiles to share.
    return f"{layer} {z}".encode() if y % 2 == 0 else f"{layer} {z}/{x}/{y}".encode()


class FakeCache:
    def __init__(self, ski_dir: Path, tile=_tile_bytes, versions=None) -> None:
        self.tile = tile
        self.versions = versions or {}
        self.stats: Counter = Counter()
        self.ski_dir = ski_dir
        self.asked: list[tuple] = []

    async def get_tile(self, layer, region, z, x, y):
        await asyncio.sleep(0)
        self.asked.append((layer, z, x, y))
        self.stats[(layer, "made")] += 1
        return self.tile(layer, z, x, y)

    async def source_version(self, layer, region):
        v = self.versions.get(layer, "v1" if LAYERS[layer].kind != "usgs" else None)
        return v() if callable(v) else v


class FakeStore:
    def __init__(self, fail_on: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_on = fail_on

    def upload(self, path, key, md5_hex, size):
        if self.fail_on and key.endswith(self.fail_on):
            raise ClientError({"Error": {"Code": "InternalError",
                                         "Message": "https://acct.r2.cloudflarestorage.com said no"}},
                              "PutObject")
        data = path.read_bytes()
        assert hashlib.md5(data).hexdigest() == md5_hex and len(data) == size
        self.objects[key] = data

    def delete(self, keys):
        self.deleted.extend(keys)
        for k in keys:
            self.objects.pop(k, None)


@pytest.fixture
def ski_dir(tmp_path):
    d = tmp_path / "ski"
    d.mkdir()
    con = sqlite3.connect(d / "ski_colorado.mbtiles")
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("INSERT INTO metadata VALUES ('json', ?)",
                (json.dumps({"vector_layers": [{"id": "ski_runs", "fields": {}}]}),))
    con.commit()
    con.close()
    return d


def _pack(session, bbox=SMALL, layers=None, created=None, **kw) -> Pack:
    pack = queue_pack(session, "colorado", bbox, layers, **kw)
    if created:
        pack.created_at = pack.queued_at = created
        session.commit()
    return pack


def _read(data: bytes) -> Reader:
    return Reader(MemorySource(data))


def _build(session, pack, cache, store, root):
    asyncio.run(build_pack(session, pack, cache, lambda: store, root))


# ── PMTiles ────────────────────────────────────────────────────────────────────

def test_small_pack_reads_back_tile_for_tile(db_session, ski_dir, tmp_path):
    pack = _pack(db_session, layers=["topo", "elevation", "ski"])
    claim_next(db_session)
    cache, store = FakeCache(ski_dir), FakeStore()
    _build(db_session, pack, cache, store, tmp_path / "build")

    assert pack.status == "ready", pack.error
    base = grid_range(SMALL)
    assert pack.tiles_total == pack.tiles_done == sum(
        len(layer_tiles(n, base)) for n in ("topo", "elevation", "ski"))
    for name in ("topo", "elevation", "ski"):
        reader = _read(store.objects[f"packs/{pack.id}/{name}.pmtiles"])
        tiles = layer_tiles(name, base)
        for z, x, y in tiles:
            want = _tile_bytes(name, z, x, y)
            assert reader.get(z, x, y) == (want or None), (name, z, x, y)
        assert reader.get(7, 26, 48) is None  # outside the layer's zooms

        header, meta = reader.header(), reader.metadata()
        spec = LAYERS[name]
        assert header["internal_compression"] == Compression.GZIP
        assert header["clustered"] is True
        written = [t for t in tiles if _tile_bytes(name, *t)]
        assert header["addressed_tiles_count"] == len(written)
        assert header["min_zoom"] == min(z for z, _, _ in written)
        assert header["max_zoom"] == spec.maxzoom
        west, _, _, north = packs.coverage(name, base).bounds()
        assert header["min_lon_e7"] == round(west * 1e7) and header["max_lat_e7"] == round(north * 1e7)
        assert meta["layer"] == name and meta["pack"] == str(pack.id)
        if name == "ski":
            assert header["tile_type"] == TileType.MVT
            assert header["tile_compression"] == Compression.GZIP
            assert meta["vector_layers"] == [{"id": "ski_runs", "fields": {}}]
        else:
            assert header["tile_compression"] == Compression.NONE
            # identical tiles are stored once
            assert header["tile_contents_count"] < header["addressed_tiles_count"]
    assert _read(store.objects[f"packs/{pack.id}/topo.pmtiles"]).header()["tile_type"] == TileType.JPEG
    assert _read(store.objects[f"packs/{pack.id}/elevation.pmtiles"]).metadata()["encoding"] == "terrarium"


def test_md5_and_bytes_recorded_match_r2(db_session, ski_dir, tmp_path):
    pack = _pack(db_session, layers=["slope", "aspect"])
    claim_next(db_session)
    store = FakeStore()
    _build(db_session, pack, FakeCache(ski_dir), store, tmp_path / "build")

    assert pack.status == "ready" and pack.built_at is not None
    assert pack.source_versions == {"slope": "v1", "aspect": "v1"}
    assert [f["layer"] for f in pack.files] == ["slope", "aspect"]
    for f in pack.files:
        data = store.objects[f["key"]]
        assert f["key"] == f"packs/{pack.id}/{f['layer']}.pmtiles"
        assert f["bytes"] == len(data) and f["md5"] == hashlib.md5(data).hexdigest()
        assert f["version"] == "v1"
    assert not (tmp_path / "build" / str(pack.id)).exists()


def test_tile_sizes_per_zoom_are_recorded(db_session, ski_dir, tmp_path):
    pack = _pack(db_session, layers=["topo", "ski"])
    claim_next(db_session)
    cache = FakeCache(ski_dir)
    _build(db_session, pack, cache, FakeStore(), tmp_path / "build")

    base = grid_range(SMALL)
    for layer in ("topo", "ski"):
        expected: dict[str, list[int]] = {}
        for z, x, y in layer_tiles(layer, base):
            slot = expected.setdefault(str(z), [0, 0])
            slot[0] += 1
            slot[1] += len(_tile_bytes(layer, z, x, y))  # empty tiles count 0 bytes
        assert pack.tile_bytes[layer] == expected
    assert pack.tiles_total == packs.tiles_total(base, ["topo", "ski"])


def test_layer_with_no_tiles_gets_no_file(db_session, ski_dir, tmp_path):
    pack = _pack(db_session, layers=["topo", "ski"])
    claim_next(db_session)
    store = FakeStore()
    cache = FakeCache(ski_dir, tile=lambda layer, z, x, y: b"" if layer == "ski" else b"jpeg")
    _build(db_session, pack, cache, store, tmp_path / "build")

    assert pack.status == "ready"
    ski = next(f for f in pack.files if f["layer"] == "ski")
    assert ski == {"layer": "ski", "version": "v1", "bytes": 0, "md5": None, "key": None}
    assert list(store.objects) == [f"packs/{pack.id}/topo.pmtiles"]


def test_writer_refuses_tiles_out_of_order(tmp_path):
    with (tmp_path / "x.pmtiles").open("wb") as f:
        writer = packs._Writer(f)
        writer.write_tile(5, b"a")
        with pytest.raises(ValueError):
            writer.write_tile(4, b"b")
        writer.tile_f.close()


def test_writer_keeps_tiles_with_equal_python_hashes_apart(tmp_path, monkeypatch):
    """pmtiles' own Writer keys tiles by hash(); a collision would give a tile
    another's bytes. Ours compares SHA-256, so it can't."""
    out = tmp_path / "x.pmtiles"
    with out.open("wb") as f:
        writer = packs._Writer(f)
        monkeypatch.setattr("builtins.hash", lambda data: 1)  # every tile "collides"
        writer.write_tile(zxy_to_tileid(1, 0, 0), b"one")
        writer.write_tile(zxy_to_tileid(1, 0, 1), b"two")
        writer.write_tile(zxy_to_tileid(1, 1, 0), b"one")
        monkeypatch.undo()
        writer.finalize(packs.pmtiles_header("topo", (-180, -85, 180, 85), (0, 0)), {})
    reader = _read(out.read_bytes())
    assert [reader.get(1, 0, 0), reader.get(1, 0, 1), reader.get(1, 1, 0)] == [b"one", b"two", b"one"]
    assert reader.header()["tile_contents_count"] == 2


# ── failures ───────────────────────────────────────────────────────────────────

def test_failed_tile_fails_the_pack_and_the_next_one_builds(db_session, ski_dir, tmp_path):
    first = _pack(db_session, layers=["topo", "slope"], created=datetime.now(UTC) - timedelta(minutes=2))
    second = _pack(db_session, layers=["topo"], created=datetime.now(UTC) - timedelta(minutes=1))
    bad = layer_tiles("slope", grid_range(SMALL))[5]

    def tile(layer, z, x, y):
        if (layer, z, x, y) == ("slope", *bad):
            raise PackTileError(f"TiTiler slope {z}/{x}/{y}: HTTPStatusError")
        return _tile_bytes(layer, z, x, y)

    store = FakeStore()
    root = tmp_path / "build"
    assert claim_next(db_session) is first
    _build(db_session, first, FakeCache(ski_dir, tile=tile), store, root)

    assert first.status == "failed"
    assert first.error == f"TiTiler slope {bad[0]}/{bad[1]}/{bad[2]}: HTTPStatusError"
    assert store.deleted == [f"packs/{first.id}/topo.pmtiles"]  # topo was uploaded, then removed
    assert store.objects == {}
    assert not (root / str(first.id)).exists()

    assert claim_next(db_session) is second
    _build(db_session, second, FakeCache(ski_dir), store, root)
    assert second.status == "ready"
    assert claim_next(db_session) is None


def test_failed_upload_keeps_r2_details_out_of_the_reason_and_log(db_session, ski_dir, tmp_path, caplog):
    pack = _pack(db_session, layers=["topo", "aspect"])
    claim_next(db_session)
    store = FakeStore(fail_on="aspect.pmtiles")
    with caplog.at_level(logging.INFO):
        _build(db_session, pack, FakeCache(ski_dir), store, tmp_path / "build")

    assert pack.status == "failed" and pack.error == "R2 error InternalError"
    # the partial upload and the one before it are deleted
    assert set(store.deleted) == {f"packs/{pack.id}/topo.pmtiles", f"packs/{pack.id}/aspect.pmtiles"}
    assert "r2.cloudflarestorage.com" not in caplog.text
    assert not (tmp_path / "build" / str(pack.id)).exists()


def test_r2_not_configured_fails_the_pack(db_session, ski_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "r2_bucket_packs", "")
    pack = _pack(db_session, layers=["topo"])
    claim_next(db_session)
    asyncio.run(build_pack(db_session, pack, FakeCache(ski_dir), R2Store, tmp_path / "build"))
    assert pack.status == "failed" and pack.error == "R2 is not configured (R2_* settings)"


def test_source_changing_mid_build_builds_the_layer_again(db_session, ski_dir, tmp_path):
    versions = iter(["v1", "v2", "v2", "v2"])
    pack = _pack(db_session, layers=["slope"])
    claim_next(db_session)
    cache = FakeCache(ski_dir, versions={"slope": lambda: next(versions)})
    _build(db_session, pack, cache, FakeStore(), tmp_path / "build")
    assert pack.status == "ready" and pack.source_versions == {"slope": "v2"}
    assert pack.tiles_done == pack.tiles_total == len(layer_tiles("slope", grid_range(SMALL)))


def test_source_changing_twice_fails(db_session, ski_dir, tmp_path):
    versions = iter(["v1", "v2", "v3", "v4"])
    pack = _pack(db_session, layers=["slope"])
    claim_next(db_session)
    cache = FakeCache(ski_dir, versions={"slope": lambda: next(versions)})
    _build(db_session, pack, cache, FakeStore(), tmp_path / "build")
    assert pack.status == "failed" and "changed during the build, twice" in pack.error


def test_shutdown_queues_the_pack_again(db_session, ski_dir, tmp_path):
    pack = _pack(db_session, layers=["topo", "slope"])
    claim_next(db_session)
    assert pack.attempts == 1
    store = FakeStore()
    started = asyncio.Event()

    def tile(layer, z, x, y):
        if layer == "slope":
            started.set()
        return _tile_bytes(layer, z, x, y)

    class SlowCache(FakeCache):
        async def get_tile(self, layer, region, z, x, y):
            data = await super().get_tile(layer, region, z, x, y)
            if layer == "slope":
                await asyncio.sleep(10)
            return data

    async def run():
        task = asyncio.ensure_future(build_pack(
            db_session, pack, SlowCache(ski_dir, tile=tile), lambda: store, tmp_path / "build"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert pack.status == "queued" and pack.attempts == 0 and pack.tiles_done == 0
    assert store.deleted == [f"packs/{pack.id}/topo.pmtiles"] and store.objects == {}
    assert not (tmp_path / "build" / str(pack.id)).exists()


# ── the queue ──────────────────────────────────────────────────────────────────

def test_claim_takes_the_oldest_and_counts_attempts(db_session):
    now = datetime.now(UTC)
    newer = _pack(db_session, created=now)
    older = _pack(db_session, created=now - timedelta(hours=1))
    assert older.status == "queued" and older.attempts == 0
    assert claim_next(db_session) is older
    assert older.status == "building" and older.attempts == 1
    assert claim_next(db_session) is newer
    assert claim_next(db_session) is None


def test_interrupted_builds_are_queued_again_then_fail_after_three(db_session):
    once = _pack(db_session)
    thrice = _pack(db_session)
    for pack, attempts in ((once, 1), (thrice, 3)):
        pack.status, pack.attempts, pack.tiles_done = "building", attempts, 10
    db_session.commit()

    assert requeue_interrupted(db_session) == (1, 1)
    assert once.status == "queued" and once.tiles_done == 0
    assert thrice.status == "failed" and thrice.error == "interrupted 3 times; not tried again"


def test_queue_pack_stores_the_box(db_session, users):
    pack = _pack(db_session, bbox=BERTHOUD, layers=["slope"], requested_by=users["owner"].id)
    db_session.expire_all()
    stored = db_session.scalars(select(Pack).where(Pack.id == pack.id)).one()
    assert packs.pack_bbox(stored) == pytest.approx(BERTHOUD)
    assert stored.layers == ["slope"] and stored.status == "queued" and stored.files == []
    assert stored.requested_by == users["owner"].id
    assert stored.last_requested_at is not None


def test_account_delete_forgets_the_requester(db_session, users, make_client):
    owner = users["owner"]
    owner.hashed_password = hash_password("pw-123456789")
    pack = _pack(db_session, requested_by=owner.id)
    other = _pack(db_session, requested_by=users["other"].id)

    r = make_client("owner").request("DELETE", "/auth/me", json={"password": "pw-123456789"})
    assert r.status_code == 204, r.text
    db_session.expire_all()
    assert db_session.get(Pack, pack.id).requested_by is None
    assert db_session.get(Pack, other.id).requested_by == users["other"].id


# ── R2 ─────────────────────────────────────────────────────────────────────────

ACCOUNT = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def r2_settings(monkeypatch):
    settings = get_settings()
    for name, value in (("r2_account_id", ACCOUNT), ("r2_access_key_id", "AKIDSECRETID"),
                        ("r2_secret_access_key", "very-secret-key"), ("r2_bucket_packs", "packs-bucket")):
        monkeypatch.setattr(settings, name, value)


def test_r2_upload_sends_md5_and_checks_the_copy(r2_settings, tmp_path, caplog):
    store = R2Store()
    assert store._s3.meta.endpoint_url == f"https://{ACCOUNT}.r2.cloudflarestorage.com"
    path = tmp_path / "topo.pmtiles"
    path.write_bytes(b"PMTiles data")
    md5, size = packs.file_md5(path)
    with Stubber(store._s3) as stub, caplog.at_level(logging.DEBUG):
        stub.add_response("put_object", {"ETag": f'"{md5}"'}, {
            "Bucket": "packs-bucket", "Key": "packs/x/topo.pmtiles", "Body": ANY,
            "ContentLength": size, "ContentType": "application/vnd.pmtiles",
            "ContentMD5": ANY,
        })
        stub.add_response("head_object", {"ETag": f'"{md5}"', "ContentLength": size},
                          {"Bucket": "packs-bucket", "Key": "packs/x/topo.pmtiles"})
        store.upload(path, "packs/x/topo.pmtiles", md5, size)
        stub.assert_no_pending_responses()
    assert "very-secret-key" not in caplog.text and "AKIDSECRETID" not in caplog.text


def test_r2_upload_with_a_different_copy_fails(r2_settings, tmp_path):
    store = R2Store()
    path = tmp_path / "topo.pmtiles"
    path.write_bytes(b"PMTiles data")
    md5, size = packs.file_md5(path)
    with Stubber(store._s3) as stub:
        stub.add_response("put_object", {"ETag": '"0"'})
        stub.add_response("head_object", {"ETag": '"0123"', "ContentLength": size})
        with pytest.raises(PackBuildError, match="doesn't match"):
            store.upload(path, "packs/x/topo.pmtiles", md5, size)


def test_r2_content_md5_is_the_files_md5(r2_settings, tmp_path):
    store = R2Store()
    path = tmp_path / "topo.pmtiles"
    path.write_bytes(b"PMTiles data")
    md5, size = packs.file_md5(path)
    sent = {}

    def capture(**kwargs):
        sent.update(kwargs)
        return {}

    store._s3 = type("S3", (), {
        "put_object": staticmethod(capture),
        "head_object": staticmethod(lambda **kw: {"ETag": f'"{md5}"', "ContentLength": size}),
    })()
    store.upload(path, "packs/x/topo.pmtiles", md5, size)
    assert base64.b64decode(sent["ContentMD5"]) == hashlib.md5(b"PMTiles data").digest()
    assert sent["ContentLength"] == size


@pytest.mark.parametrize("value, message", [
    (f"https://{ACCOUNT}.r2.cloudflarestorage.com", "the 32-character account ID, not the endpoint URL"),
    (f"{ACCOUNT}.r2.cloudflarestorage.com", "not the endpoint URL"),
    (ACCOUNT[:-1], "must be the 32-character account ID"),
    (ACCOUNT.upper(), "must be the 32-character account ID"),
])
def test_bad_account_id_is_refused_without_echoing_it(r2_settings, monkeypatch, value, message):
    monkeypatch.setattr(get_settings(), "r2_account_id", value)
    with pytest.raises(PackBuildError, match=message) as info:
        R2Store()
    assert value not in str(info.value)


def test_bad_account_id_fails_the_pack_before_any_tile(db_session, ski_dir, tmp_path, r2_settings, monkeypatch):
    monkeypatch.setattr(get_settings(), "r2_account_id", f"https://{ACCOUNT}.r2.cloudflarestorage.com")
    pack = _pack(db_session, layers=["topo"])
    claim_next(db_session)
    cache = FakeCache(ski_dir)
    asyncio.run(build_pack(db_session, pack, cache, R2Store, tmp_path / "build"))
    assert pack.status == "failed" and "not the endpoint URL" in pack.error
    assert cache.asked == []


def test_worker_with_a_bad_account_id_stops_at_start(r2_settings, monkeypatch, caplog):
    from app.jobs import pack_worker

    monkeypatch.setattr(get_settings(), "r2_account_id", f"https://{ACCOUNT}.r2.cloudflarestorage.com")
    monkeypatch.setattr(pack_worker, "_take_lock", lambda: pytest.fail("got past the settings check"))
    assert pack_worker._serve() == 1
    assert pack_worker._once() == 1
    assert "not the endpoint URL" in caplog.text and ACCOUNT not in caplog.text


def test_reason_drops_botocore_messages():
    exc = ClientError({"Error": {"Code": "AccessDenied",
                                 "Message": "https://acct.r2.cloudflarestorage.com/bucket"}}, "PutObject")
    assert packs.reason(exc) == "R2 error AccessDenied"
    assert packs.reason(RuntimeError("secret")) == "unexpected error RuntimeError"


# ── the worker loop ────────────────────────────────────────────────────────────

def test_worker_builds_queued_packs_until_stopped(db_session, ski_dir, tmp_path, monkeypatch):
    from app.jobs import pack_worker

    class Cache(FakeCache):
        async def aclose(self):
            pass

    store = FakeStore()
    monkeypatch.setattr(pack_worker, "open_session", lambda: db_session)
    monkeypatch.setattr(pack_worker, "PackCache", lambda: Cache(ski_dir))
    monkeypatch.setattr(pack_worker.packs, "R2Store", lambda: store)
    monkeypatch.setattr(pack_worker, "POLL_S", 0.01)
    monkeypatch.setattr(get_settings(), "pack_cache_dir", str(tmp_path / "cache"))
    stale = (tmp_path / "cache" / "build" / "old")
    stale.mkdir(parents=True)
    interrupted = _pack(db_session, layers=["topo"], created=datetime.now(UTC) - timedelta(hours=1))
    interrupted.status, interrupted.attempts = "building", 1
    db_session.commit()
    fresh = _pack(db_session, layers=["slope"])
    ids = interrupted.id, fresh.id

    async def run():
        stop = asyncio.Event()
        worker = asyncio.ensure_future(pack_worker.run(stop))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if fresh.status == "ready":
                break
        stop.set()
        await asyncio.wait_for(worker, 5)

    asyncio.run(run())
    interrupted, fresh = (db_session.get(Pack, i) for i in ids)  # the worker closed its session
    assert interrupted.status == "ready" and interrupted.attempts == 2
    assert fresh.status == "ready" and fresh.attempts == 1
    assert sorted(store.objects) == sorted([f"packs/{interrupted.id}/topo.pmtiles",
                                           f"packs/{fresh.id}/slope.pmtiles"])
    assert not stale.exists()
