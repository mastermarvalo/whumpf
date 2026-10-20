"""Offline pack endpoints (app/routers/packs.py, docs/api-packs.md): sign-in,
region access, box and layer checks, one id per request, sharing, the rate,
per-user and queue limits, links only when ready, rebuilding when R2 has
lost the files, the cleanup and account deletion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth.service import hash_password
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Pack, PackRequest
from app.routers import packs as packs_router
from app.services import packs
from app.services.pack_cache import PackTileError

BERTHOUD = [-105.835, 39.753, -105.719, 39.843]
REQUIRED = ["topo", "slope", "aspect", "elevation", "ski"]
ALL = ["topo", "imagery", "slope", "aspect", "elevation", "ski"]


def _box(i: int) -> list[float]:
    """A different ~2 km box for each i, all inside Colorado."""
    west = -106.9 + 0.05 * i
    return [west, 39.5, west + 0.02, 39.52]


class FakeVersions:
    def __init__(self) -> None:
        self.values = {"slope": "s1", "aspect": "a1", "elevation": "e1|d1", "ski": "k1"}
        self.broken: set[str] = set()

    async def layer_version(self, layer, region):
        if layer in self.broken:
            raise PackTileError(f"can't read the version of {layer}")
        return self.values.get(layer)


class FakeR2:
    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.heads: list[str] = []
        self.fail = False

    def exists(self, key):
        self.heads.append(key)
        if self.fail:
            raise ClientError({"Error": {"Code": "InternalError", "Message": "no"}}, "HeadObject")
        return key in self.keys

    def presign(self, key, ttl_s=packs.URL_TTL_S):
        return f"https://r2.example/{key}?X-Amz-Expires={ttl_s}&X-Amz-Signature=sig"


@pytest.fixture
def versions(monkeypatch):
    fake = FakeVersions()
    monkeypatch.setattr(packs_router, "_versions", fake)
    return fake


@pytest.fixture
def r2(monkeypatch):
    fake = FakeR2()
    monkeypatch.setattr(packs_router, "_store", lambda: fake)
    settings = get_settings()
    for name, value in (("r2_account_id", "0123456789abcdef0123456789abcdef"),
                        ("r2_access_key_id", "AKID"), ("r2_secret_access_key", "secret"),
                        ("r2_bucket_packs", "packs-bucket"), ("usgs_contact", "test@example.com")):
        monkeypatch.setattr(settings, name, value)
    return fake


@pytest.fixture(autouse=True)
def fresh_averages(monkeypatch):
    monkeypatch.setattr(packs_router, "_averages", {})


@pytest.fixture
def client(make_client, versions, r2):
    return make_client


def _post(c: TestClient, bbox=BERTHOUD, layers=ALL, region="colorado"):
    return c.post("/packs", json={"region": region, "bbox": bbox, "layers": layers})


def _make_ready(session, pack_id, r2: FakeR2, built_at=None) -> Pack:
    pack = session.get(Pack, pack_id)
    pack.status = "ready"
    pack.built_at = built_at or datetime.now(UTC) - timedelta(hours=1)
    pack.tiles_done = pack.tiles_total
    pack.files = []
    for name in pack.layers:
        key = None if name == "ski" else f"packs/{pack.id}/{name}.pmtiles"
        pack.files.append({"layer": name, "version": None, "bytes": 0 if key is None else 1000,
                           "md5": None if key is None else "0" * 32, "key": key})
        if key:
            r2.keys.add(key)
    session.commit()
    return pack


# ── sign-in and region ─────────────────────────────────────────────────────────

def test_signed_out_gets_401(db_session, versions, r2):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        c = TestClient(app)
        assert c.get("/packs/manifest?region=colorado").status_code == 401
        assert _post(c).status_code == 401
        assert c.get("/packs/3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_region_must_exist_and_be_allowed(client, users, db_session):
    c = client("owner")
    assert _post(c, region="narnia").status_code == 400
    assert c.get("/packs/manifest?region=narnia").status_code == 400
    users["owner"].allowed_regions = "utah"
    db_session.commit()
    r = _post(c)
    assert r.status_code == 403 and r.json()["detail"] == "Region not available for this account"
    assert c.get("/packs/manifest?region=colorado").status_code == 403


# ── manifest ───────────────────────────────────────────────────────────────────

def test_manifest(client, versions):
    r = client("owner").get("/packs/manifest?region=colorado")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["max_area_km2"] == 700 and body["max_side_km"] == 50 and body["grid_zoom"] == 14
    assert body["required_layers"] == REQUIRED and body["optional_layers"] == ["imagery"]
    assert body["url_ttl_s"] == 86400
    assert body["limits"] == {"requests_per_hour": 10, "active_per_user": 2, "polls_per_minute": 60}
    layers = {layer["id"]: layer for layer in body["layers"]}
    assert list(layers) == ALL
    assert layers["topo"]["source_version"] is None and layers["topo"]["available"] is True
    assert layers["topo"]["bytes_per_tile"]["16"] == 13086
    assert layers["elevation"]["pad_tiles"] == 2 and layers["elevation"]["encoding"] == "terrarium"
    assert layers["elevation"]["source_version"] == "e1|d1"
    assert layers["slope"]["tile_size"] == 512 and layers["slope"]["bytes_per_tile"]["14"] == 66930
    assert layers["ski"]["tile_type"] == "mvt" and layers["ski"]["attribution"] == "© OpenStreetMap contributors"


def test_manifest_marks_layers_that_cant_be_built(client, versions, monkeypatch):
    versions.broken.add("ski")
    monkeypatch.setattr(get_settings(), "usgs_contact", "")
    layers = {layer["id"]: layer for layer in client("owner").get("/packs/manifest?region=colorado").json()["layers"]}
    assert not layers["topo"]["available"] and not layers["imagery"]["available"]
    assert not layers["ski"]["available"] and layers["ski"]["source_version"] is None
    assert layers["slope"]["available"]


def test_manifest_pools_built_packs_with_the_seed(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    pack = _make_ready(db_session, pid, r2)
    pack.tile_bytes = {"topo": {"16": [672, 672 * 23086]}}  # the seed is 672 tiles of 13086
    db_session.commit()
    layers = {layer["id"]: layer for layer in c.get("/packs/manifest?region=colorado").json()["layers"]}
    assert layers["topo"]["bytes_per_tile"]["16"] == 18086
    assert layers["topo"]["bytes_per_tile"]["15"] == 19662


# ── POST /packs ────────────────────────────────────────────────────────────────

def test_new_request_is_queued(client, db_session, users):
    r = _post(client("owner"))
    assert r.status_code == 202, r.text
    body = r.json()
    base = packs.grid_range(tuple(BERTHOUD))
    assert body["status"] == "queued" and body["layers"] == ALL and body["region"] == "colorado"
    assert body["bounds"] == [round(v, 5) for v in base.bounds()]
    assert body["tiles_total"] == packs.tiles_total(base, ALL) == 2218
    assert body["progress"] == 0.0 and body["tiles_done"] == 0 and body["queue_position"] == 1
    assert body["files"] == [] and body["error"] is None
    assert body["built_at"] is None and body["urls_expire_at"] is None
    pack = db_session.get(Pack, uuid.UUID(body["id"]))
    assert pack.requested_by == users["owner"].id
    # The rounded box is stored, not the one asked for.
    assert packs.pack_bbox(pack) == pytest.approx(base.bounds())


def test_same_request_same_id_for_everyone(client, db_session, users):
    first = _post(client("owner")).json()["id"]
    again = _post(client("owner"), layers=list(reversed(ALL)))
    assert again.status_code == 200 and again.json()["id"] == first
    # A box that rounds to the same zoom 14 tiles is the same pack.
    nudged = [BERTHOUD[0] + 0.001, BERTHOUD[1], BERTHOUD[2] - 0.001, BERTHOUD[3]]
    other = _post(client("other"), bbox=nudged)
    assert other.status_code == 200 and other.json()["id"] == first
    assert db_session.scalar(select(func.count()).select_from(Pack)) == 1
    assert db_session.scalar(select(func.count()).select_from(PackRequest)) == 2


def test_different_layers_or_versions_are_another_pack(client, versions):
    c = client("owner")
    first = _post(c).json()["id"]
    assert _post(c, layers=REQUIRED).json()["id"] != first
    versions.values["ski"] = "k2"
    assert _post(client("other")).json()["id"] != first


def test_ids_cant_be_worked_out_without_the_secret(monkeypatch):
    base = packs.grid_range(tuple(BERTHOUD))
    v = {"slope": "s1"}
    one = packs.pack_id("colorado", base, ["slope"], v)
    assert one == packs.pack_id("colorado", base, ["slope"], v)
    monkeypatch.setattr(get_settings(), "jwt_secret", "another-secret-of-enough-length")
    assert packs.pack_id("colorado", base, ["slope"], v) != one


@pytest.mark.parametrize("bbox, message", [
    ([-106.0, 39.7, -105.6, 39.9], "the most is 700 km²"),
    ([-106.5, 39.70, -105.8, 39.72], "the most is 50 km on a side"),
    ([-105.8, 39.7, -105.7], "bbox must be four numbers: west, south, east, north"),
    ([-105.7, 39.7, -105.8, 39.8], "bbox must have west < east and south < north"),
    ([-104.4, 39.7, -104.3, 39.8], "bbox must be inside the colorado region"),
])
def test_box_limits(client, bbox, message):
    r = _post(client("owner"), bbox=bbox)
    assert r.status_code == 422 and message in r.json()["detail"]


def test_700_km2_is_allowed(client):
    # ~26.4 km on a side
    side = 26.3
    lat = 39.5
    dlat = side / 110.574
    dlon = side / (111.32 * 0.7716)
    assert _post(client("owner"), bbox=[-106.5, lat, -106.5 + dlon, lat + dlat]).status_code == 202


@pytest.mark.parametrize("layers", [["topo", "slope"], ALL + ["roads"], ["imagery"]])
def test_layers_must_be_the_required_ones(client, layers):
    r = _post(client("owner"), layers=layers)
    assert r.status_code == 422
    assert r.json()["detail"] == "layers must include topo, slope, aspect, elevation and ski; imagery is optional"


def test_unavailable_sources_answer_503(client, versions, monkeypatch, db_session):
    versions.broken.add("elevation")
    r = _post(client("owner"))
    assert r.status_code == 503 and r.headers["retry-after"] == "60"
    assert r.json()["detail"] == "Offline packs are not available right now."
    versions.broken.clear()
    monkeypatch.setattr(get_settings(), "r2_bucket_packs", "")
    assert _post(client("owner")).status_code == 503
    assert db_session.scalar(select(func.count()).select_from(Pack)) == 0


# ── limits ─────────────────────────────────────────────────────────────────────

def test_ten_requests_an_hour(client, db_session):
    c = client("owner")
    for _ in range(10):
        assert _post(c).status_code in (200, 202)
    r = _post(c)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert _post(client("other")).status_code == 200  # per user


def test_two_active_packs_per_user(client, db_session):
    # make_client switches who is signed in for every client, so make one per call.
    def owner():
        return client("owner")

    assert _post(owner(), bbox=_box(0)).status_code == 202
    assert _post(owner(), bbox=_box(1)).status_code == 202
    r = _post(owner(), bbox=_box(2))
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert r.json()["detail"] == "You already have 2 packs being prepared. Try again when one is ready."
    # Asking again for one of the two is fine.
    assert _post(owner(), bbox=_box(1)).status_code == 200
    # Joining someone else's build counts too.
    assert _post(client("other"), bbox=_box(3)).status_code == 202
    assert _post(owner(), bbox=_box(3)).status_code == 429
    # A ready one doesn't count.
    first = db_session.scalars(select(PackRequest.pack_id).order_by(PackRequest.requested_at)).first()
    db_session.get(Pack, first).status = "ready"
    db_session.commit()
    assert _post(owner(), bbox=_box(2)).status_code == 202


def test_queue_cap(client, db_session, users):
    for i in range(packs.MAX_ACTIVE):
        packs.queue_pack(db_session, "colorado", _box(i), ["slope"])
    r = _post(client("owner"))
    assert r.status_code == 503 and r.headers["retry-after"] == "300"
    assert r.json()["detail"] == "Too many packs are being prepared right now. Try again in a few minutes."
    assert db_session.scalar(select(func.count()).select_from(PackRequest)) == 0


def test_queue_cap_never_refuses_a_pack_already_queued(client, db_session):
    queued = _post(client("owner")).json()["id"]
    for i in range(packs.MAX_ACTIVE):
        packs.queue_pack(db_session, "colorado", _box(i), ["slope"])
    r = _post(client("other"))
    assert r.status_code == 200 and r.json()["id"] == queued


def test_polling_limit(client):
    c = client("owner")
    pid = _post(c).json()["id"]
    for _ in range(60):
        assert c.get(f"/packs/{pid}").status_code == 200
    r = c.get(f"/packs/{pid}")
    assert r.status_code == 429 and "retry-after" in r.headers


# ── GET /packs/{id} ────────────────────────────────────────────────────────────

def test_only_those_who_asked_can_see_a_pack(client):
    pid = _post(client("owner")).json()["id"]
    r = client("other").get(f"/packs/{pid}")
    assert r.status_code == 404 and r.json()["detail"] == "Pack not found"
    assert client("owner").get("/packs/not-a-uuid").status_code == 404
    assert client("owner").get("/packs/3f2a9c1e-8b47-4d0a-9e21-6c5f0b7d2a14").status_code == 404


def test_progress_while_building(client, db_session):
    c = client("owner")
    pid = _post(c).json()["id"]
    pack = db_session.get(Pack, uuid.UUID(pid))
    pack.status, pack.tiles_done = "building", 812
    db_session.commit()
    body = c.get(f"/packs/{pid}").json()
    assert body["status"] == "building" and body["progress"] == 0.366
    assert body["tiles_done"] == 812 and body["queue_position"] is None and body["files"] == []


def test_queue_position(client, db_session):
    first = _post(client("owner"), bbox=_box(0)).json()["id"]
    second = _post(client("other"), bbox=_box(1)).json()
    assert second["queue_position"] == 2
    db_session.get(Pack, uuid.UUID(first)).status = "building"
    db_session.commit()
    assert client("other").get(f"/packs/{second['id']}").json()["queue_position"] == 1


def test_urls_only_when_ready(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    assert c.get(f"/packs/{pid}").json()["files"] == []
    _make_ready(db_session, uuid.UUID(pid), r2)
    r = c.get(f"/packs/{pid}")
    body = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert body["status"] == "ready" and body["progress"] == 1.0 and body["error"] is None
    assert body["built_at"].endswith("Z") and body["urls_expire_at"].endswith("Z")
    expires = datetime.fromisoformat(body["urls_expire_at"])
    assert timedelta(hours=23, minutes=59) < expires - datetime.now(UTC) <= timedelta(hours=24)
    files = {f["layer"]: f for f in body["files"]}
    assert list(files) == ALL
    assert files["topo"]["url"] == f"https://r2.example/packs/{pid}/topo.pmtiles?X-Amz-Expires=86400&X-Amz-Signature=sig"
    assert files["topo"]["bytes"] == 1000 and files["topo"]["md5"] == "0" * 32
    assert files["ski"] == {"layer": "ski", "version": None, "bytes": 0, "md5": None, "url": None}
    # A ready pack is answered at once to anyone asking for it.
    again = _post(client("other"))
    assert again.status_code == 200 and again.json()["status"] == "ready" and again.json()["files"][0]["url"]


def test_failed_pack_shows_a_plain_error_and_a_post_queues_it_again(client, db_session):
    c = client("owner")
    pid = _post(c).json()["id"]
    pack = db_session.get(Pack, uuid.UUID(pid))
    pack.status, pack.error, pack.attempts = "failed", "TiTiler slope 14/1/2: ConnectError('http://titiler:8000')", 3
    db_session.commit()
    body = c.get(f"/packs/{pid}").json()
    assert body["status"] == "failed" and body["error"] == "This pack couldn't be built. Try again later."
    r = _post(c)
    assert r.status_code == 202 and r.json()["id"] == pid and r.json()["status"] == "queued"
    assert pack.attempts == 0 and pack.error is None


# ── files gone from R2 ─────────────────────────────────────────────────────────

def test_get_queues_a_pack_again_when_a_file_is_gone(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    pack = _make_ready(db_session, uuid.UUID(pid), r2)
    r2.keys.discard(f"packs/{pid}/slope.pmtiles")
    body = c.get(f"/packs/{pid}").json()
    assert body["status"] == "queued" and body["files"] == [] and body["queue_position"] == 1
    assert pack.built_at is None and pack.files == [] and pack.attempts == 0


def test_post_queues_a_pack_again_when_a_file_is_gone(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    _make_ready(db_session, uuid.UUID(pid), r2)
    r2.keys.clear()
    r = _post(client("other"))
    assert r.status_code == 202 and r.json()["id"] == pid and r.json()["status"] == "queued"


def test_a_pack_older_than_29_days_is_built_again_without_asking_r2(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    _make_ready(db_session, uuid.UUID(pid), r2, built_at=datetime.now(UTC) - timedelta(days=29, minutes=1))
    r2.heads.clear()
    assert c.get(f"/packs/{pid}").json()["status"] == "queued"
    assert r2.heads == []


def test_rebuild_respects_the_queue_cap(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    _make_ready(db_session, uuid.UUID(pid), r2)
    r2.keys.clear()
    for i in range(packs.MAX_ACTIVE):
        packs.queue_pack(db_session, "colorado", _box(i), ["slope"])
    assert c.get(f"/packs/{pid}").status_code == 503
    assert db_session.get(Pack, uuid.UUID(pid)).status == "ready"


def test_r2_unreachable_is_503_and_queues_nothing(client, db_session, r2):
    c = client("owner")
    pid = _post(c).json()["id"]
    _make_ready(db_session, uuid.UUID(pid), r2)
    r2.fail = True
    r = c.get(f"/packs/{pid}")
    assert r.status_code == 503 and r.json()["detail"] == "Offline packs are not available right now."
    assert db_session.get(Pack, uuid.UUID(pid)).status == "ready"


# ── cleanup and account deletion ───────────────────────────────────────────────

def test_cleanup_forgets_old_packs(client, db_session, r2):
    now = datetime.now(UTC)
    ids = {name: uuid.UUID(_post(client(user), bbox=_box(i)).json()["id"])
           for i, (name, user) in enumerate((("old_ready", "owner"), ("new_ready", "owner"),
                                              ("old_failed", "other"), ("new_failed", "other"),
                                              ("old_building", "third")))}
    for name, status, age in (("old_ready", "ready", timedelta(days=61)), ("new_ready", "ready", timedelta(days=59)),
                              ("old_failed", "failed", timedelta(days=1, minutes=1)),
                              ("new_failed", "failed", timedelta(hours=23)),
                              ("old_building", "building", timedelta(days=90))):
        pack = db_session.get(Pack, ids[name])
        pack.status, pack.last_requested_at = status, now - age
    db_session.commit()

    assert packs.cleanup(db_session, now) == 2
    left = set(db_session.scalars(select(Pack.id)).all())
    assert left == {ids["new_ready"], ids["new_failed"], ids["old_building"]}
    assert set(db_session.scalars(select(PackRequest.pack_id)).all()) == left


def test_account_delete_drops_the_users_requests(client, db_session, users):
    owner = users["owner"]
    owner.hashed_password = hash_password("pw-123456789")
    db_session.commit()
    pid = _post(client("owner")).json()["id"]
    _post(client("other"))
    r = client("owner").request("DELETE", "/auth/me", json={"password": "pw-123456789"})
    assert r.status_code == 204, r.text
    assert db_session.scalars(select(PackRequest.user_id)).all() == [users["other"].id]
    assert db_session.get(Pack, uuid.UUID(pid)) is not None
