"""Auth endpoints: register, token (login), and current-user."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.service import create_access_token, hash_password, verify_password
from app.db import get_session
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("whumpf.auth")


class RegisterIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: str

    model_config = {"from_attributes": True}


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, session: Session = Depends(get_session)) -> TokenOut:
    email = body.email.lower().strip()
    if "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid email address")
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")
    if session.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")

    user = User(email=email, hashed_password=hash_password(body.password))
    session.add(user)
    session.commit()
    logger.info("New user registered: %s", email)
    return TokenOut(access_token=create_access_token(user.email))


@router.post("/token", response_model=TokenOut)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> TokenOut:
    email = form.username.lower().strip()
    user = session.scalars(select(User).where(User.email == email)).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(access_token=create_access_token(user.email))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return user
