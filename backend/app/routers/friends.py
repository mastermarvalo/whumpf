"""Friends — mutual friend requests (add by email, accept/decline).

A private party graph used to invite people to trips. No name search / discovery
(brief non-goal) — you add someone by their email, and they must accept.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db import get_session
from app.models.friendship import Friendship, FriendStatus
from app.models.user import User
from app.services.email import send_friend_request

router = APIRouter(prefix="/friends", tags=["friends"])
logger = logging.getLogger("whumpf.friends")

SessionDep = Annotated[Session, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


class FriendRequestIn(BaseModel):
    email: str


class RespondIn(BaseModel):
    action: Literal["accept", "decline"]


class FriendOut(BaseModel):
    friendship_id: int
    user_id: int
    email: str


class FriendsOut(BaseModel):
    friends: list[FriendOut]
    incoming: list[FriendOut]   # pending requests TO me (I can accept/decline)
    outgoing: list[FriendOut]   # pending requests I sent


def _existing_between(session: Session, a_id: int, b_id: int) -> Friendship | None:
    return session.scalars(
        select(Friendship).where(
            or_(
                and_(Friendship.requester_id == a_id, Friendship.addressee_id == b_id),
                and_(Friendship.requester_id == b_id, Friendship.addressee_id == a_id),
            )
        )
    ).first()


@router.post("/request", status_code=201)
async def send_request(payload: FriendRequestIn, session: SessionDep, user: UserDep) -> dict:
    email = payload.email.lower().strip()
    target = session.scalars(select(User).where(User.email == email)).first()
    if target is None:
        raise HTTPException(404, "No whumpf user with that email")
    if target.id == user.id:
        raise HTTPException(400, "You can't friend yourself")

    existing = _existing_between(session, user.id, target.id)
    if existing is not None:
        if existing.status == FriendStatus.accepted:
            raise HTTPException(400, "Already friends")
        raise HTTPException(400, "A friend request is already pending")

    fr = Friendship(requester_id=user.id, addressee_id=target.id, status=FriendStatus.pending)
    session.add(fr)
    session.commit()
    try:
        await send_friend_request(to=target.email, requester_email=user.email)
    except Exception as exc:
        logger.warning("Friend-request email failed for %s: %s", target.email, exc)
    return {"status": "requested"}


@router.post("/{friendship_id}/respond", status_code=204)
def respond(
    friendship_id: int, payload: RespondIn, session: SessionDep, user: UserDep,
) -> None:
    fr = session.get(Friendship, friendship_id)
    # Only the addressee of a pending request may respond.
    if fr is None or fr.addressee_id != user.id or fr.status != FriendStatus.pending:
        raise HTTPException(404, "Friend request not found")
    if payload.action == "accept":
        fr.status = FriendStatus.accepted
        fr.responded_at = datetime.now(timezone.utc)
    else:
        session.delete(fr)
    session.commit()


@router.delete("/{friendship_id}", status_code=204)
def remove_friend(friendship_id: int, session: SessionDep, user: UserDep) -> None:
    fr = session.get(Friendship, friendship_id)
    if fr is None or user.id not in (fr.requester_id, fr.addressee_id):
        raise HTTPException(404, "Friendship not found")
    session.delete(fr)
    session.commit()


@router.get("", response_model=FriendsOut)
def list_friends(session: SessionDep, user: UserDep) -> FriendsOut:
    rows = session.scalars(
        select(Friendship).where(
            or_(Friendship.requester_id == user.id, Friendship.addressee_id == user.id)
        )
    ).all()

    # Resolve the "other" user's email for each row in one lookup.
    other_ids = {
        (r.addressee_id if r.requester_id == user.id else r.requester_id) for r in rows
    }
    emails = {
        u.id: u.email
        for u in session.scalars(select(User).where(User.id.in_(other_ids or {0}))).all()
    }

    friends, incoming, outgoing = [], [], []
    for r in rows:
        other_id = r.addressee_id if r.requester_id == user.id else r.requester_id
        entry = FriendOut(friendship_id=r.id, user_id=other_id, email=emails.get(other_id, ""))
        if r.status == FriendStatus.accepted:
            friends.append(entry)
        elif r.addressee_id == user.id:
            incoming.append(entry)
        else:
            outgoing.append(entry)
    return FriendsOut(friends=friends, incoming=incoming, outgoing=outgoing)
