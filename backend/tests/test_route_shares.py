"""Phase B: route sharing, token access, and clone — authorization focus.

Terrain sampling is mocked so these run without COG/network access.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.route_share import RouteShare
from app.services.cog_sampler import TerrainSample

_LINE = {
    "type": "LineString",
    "coordinates": [[-106.40, 39.10], [-106.39, 39.11], [-106.38, 39.115]],
}


@pytest.fixture(autouse=True)
def _mock_sampling(monkeypatch):
    def _fake(vertices, region, settings, n_total=128):
        out = []
        for i in range(n_total):
            frac = i / (n_total - 1)
            out.append(
                TerrainSample(
                    distance_m=frac * 1500.0,
                    elevation_m=3000.0 + frac * 200.0,
                    slope_deg=10.0 + frac * 25.0,
                    aspect_deg=(frac * 360.0) % 360.0,
                )
            )
        return out

    monkeypatch.setattr("app.routers.routes.sample_polyline", _fake)


def _create(client, name="Sharable", region="colorado", geometry=None):
    return client.post(
        "/routes", json={"name": name, "region": region, "geometry": geometry or _LINE}
    )


def _share(client, route_id):
    return client.post(f"/routes/{route_id}/share")


def test_owner_share_is_idempotent(make_client, db_session):
    client = make_client("owner")
    rid = _create(client).json()["id"]

    r1 = _share(client, rid)
    assert r1.status_code == 201
    token = r1.json()["token"]
    assert token

    r2 = _share(client, rid)
    assert r2.status_code == 200
    assert r2.json()["token"] == token  # same active link

    active = db_session.scalars(
        select(RouteShare).where(
            RouteShare.route_id == rid, RouteShare.revoked_at.is_(None)
        )
    ).all()
    assert len(active) == 1


def test_share_flips_visibility_to_unlisted(make_client):
    client = make_client("owner")
    rid = _create(client).json()["id"]
    assert client.get(f"/routes/{rid}").json()["visibility"] == "private"
    _share(client, rid)
    assert client.get(f"/routes/{rid}").json()["visibility"] == "unlisted"


def test_non_owner_cannot_share_or_revoke(make_client):
    rid = _create(make_client("owner")).json()["id"]
    other = make_client("other")
    assert other.post(f"/routes/{rid}/share").status_code == 403
    assert other.delete(f"/routes/{rid}/share/whatever").status_code == 403


def test_view_requires_valid_token(make_client):
    owner = make_client("owner")
    rid = _create(owner).json()["id"]
    token = _share(owner, rid).json()["token"]

    other = make_client("other")
    assert other.get(f"/routes/{rid}").status_code == 404           # no token
    assert other.get(f"/routes/{rid}?token={token}").status_code == 200


def test_token_is_scoped_to_its_route(make_client):
    owner = make_client("owner")
    rid1 = _create(owner, name="A").json()["id"]
    rid2 = _create(owner, name="B").json()["id"]
    token1 = _share(owner, rid1).json()["token"]

    other = make_client("other")
    # A's token must not unlock B.
    assert other.get(f"/routes/{rid2}?token={token1}").status_code == 404


def test_revoked_token_denies_view_and_clone(make_client):
    owner = make_client("owner")
    rid = _create(owner).json()["id"]
    token = _share(owner, rid).json()["token"]
    assert owner.delete(f"/routes/{rid}/share/{token}").status_code == 204

    other = make_client("other")
    assert other.get(f"/routes/{rid}?token={token}").status_code == 404
    assert other.post(f"/routes/{rid}/clone?token={token}").status_code == 404


def test_clone_via_token_creates_owned_private_copy(make_client, users):
    owner = make_client("owner")
    src = _create(owner).json()
    rid = src["id"]
    token = _share(owner, rid).json()["token"]

    other = make_client("other")
    resp = other.post(f"/routes/{rid}/clone?token={token}")
    assert resp.status_code == 201, resp.text
    clone = resp.json()

    assert clone["owner_id"] == users["other"].id
    assert clone["name"].endswith("(copy)")
    assert clone["visibility"] == "private"
    assert clone["id"] != rid
    # Deep copy of the cached profile/geometry (no re-sampling).
    assert len(clone["geometry"]["coordinates"]) == len(src["geometry"]["coordinates"])
    assert clone["summary"]["distance_m"] == src["summary"]["distance_m"]
    assert len(clone["samples"]) == len(other.get(f"/routes/{clone['id']}").json()["samples"])

    # Clone is in the cloner's list, not the owner's. (make_client overrides the
    # current user app-globally, so re-bind owner before querying as the owner.)
    assert any(r["id"] == clone["id"] for r in other.get("/routes").json())
    owner = make_client("owner")
    assert all(r["id"] != clone["id"] for r in owner.get("/routes").json())


def test_clone_without_access_is_404(make_client):
    rid = _create(make_client("owner")).json()["id"]
    assert make_client("other").post(f"/routes/{rid}/clone").status_code == 404


def test_owner_can_clone_own_route(make_client):
    owner = make_client("owner")
    rid = _create(owner).json()["id"]
    assert owner.post(f"/routes/{rid}/clone").status_code == 201


def test_revoke_resets_visibility_to_private(make_client):
    owner = make_client("owner")
    rid = _create(owner).json()["id"]
    token = _share(owner, rid).json()["token"]
    assert owner.get(f"/routes/{rid}").json()["visibility"] == "unlisted"

    owner.delete(f"/routes/{rid}/share/{token}")
    assert owner.get(f"/routes/{rid}").json()["visibility"] == "private"


def test_revoke_unknown_token_is_404(make_client):
    owner = make_client("owner")
    rid = _create(owner).json()["id"]
    assert owner.delete(f"/routes/{rid}/share/bogus-token").status_code == 404
