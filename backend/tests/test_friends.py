"""Phase C: friends — mutual friend requests, accept/decline, listing."""

from __future__ import annotations

from sqlalchemy import select

from app.models.friendship import Friendship


def _req(client, email):
    return client.post("/friends/request", json={"email": email})


def test_request_resolves_email_and_lists(make_client, users):
    owner = make_client("owner")
    assert _req(owner, "other@example.com").status_code == 201

    # Owner sees an outgoing pending request.
    listed = owner.get("/friends").json()
    assert len(listed["outgoing"]) == 1
    assert listed["outgoing"][0]["email"] == "other@example.com"
    assert listed["friends"] == []

    # The addressee sees it as incoming.
    other = make_client("other")
    inc = other.get("/friends").json()["incoming"]
    assert len(inc) == 1 and inc[0]["email"] == "owner@example.com"


def test_cannot_friend_self_or_unknown(make_client):
    owner = make_client("owner")
    assert _req(owner, "owner@example.com").status_code == 400
    assert _req(owner, "nobody@example.com").status_code == 404


def test_request_is_idempotent(make_client):
    owner = make_client("owner")
    assert _req(owner, "other@example.com").status_code == 201
    assert _req(owner, "other@example.com").status_code == 400  # already pending


def test_accept_makes_both_friends(make_client):
    owner = make_client("owner")
    _req(owner, "other@example.com")
    other = make_client("other")
    fid = other.get("/friends").json()["incoming"][0]["friendship_id"]
    assert other.post(f"/friends/{fid}/respond", json={"action": "accept"}).status_code == 204

    assert any(f["email"] == "owner@example.com" for f in other.get("/friends").json()["friends"])
    owner = make_client("owner")
    assert any(f["email"] == "other@example.com" for f in owner.get("/friends").json()["friends"])


def test_decline_removes_request(make_client, users, db_session):
    owner = make_client("owner")
    _req(owner, "other@example.com")
    other = make_client("other")
    fid = other.get("/friends").json()["incoming"][0]["friendship_id"]
    assert other.post(f"/friends/{fid}/respond", json={"action": "decline"}).status_code == 204
    # Scope to this pair — the shared dev DB may hold unrelated real rows.
    remaining = db_session.scalars(
        select(Friendship).where(
            Friendship.requester_id == users["owner"].id,
            Friendship.addressee_id == users["other"].id,
        )
    ).all()
    assert remaining == []


def test_non_addressee_cannot_respond(make_client):
    owner = make_client("owner")
    _req(owner, "other@example.com")
    fid = owner.get("/friends").json()["outgoing"][0]["friendship_id"]
    # The requester (owner) cannot accept their own request.
    third = make_client("third")
    assert third.post(f"/friends/{fid}/respond", json={"action": "accept"}).status_code == 404
    owner = make_client("owner")
    assert owner.post(f"/friends/{fid}/respond", json={"action": "accept"}).status_code == 404
