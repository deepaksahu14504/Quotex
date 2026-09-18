"""App-level user accounts (separate from Quotex broker login) + JWT auth.

Every API route (except /api/auth/*) requires a valid Bearer token. The
WebSocket endpoint accepts the token as a `?token=` query param since browsers
can't set custom headers on a WS handshake.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text

from . import db
from .rate_limit import RateLimiter
from .security import create_token, decode_token, hash_password, verify_password, TokenError

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Critical fix: login previously had no brute-force protection at all --
# unlimited password guesses against any account. Two independent limiters:
# by IP (catches one attacker spraying many emails) and by email (catches a
# distributed attacker -- many IPs -- targeting one specific account). Both
# must pass for a login attempt to proceed.
_login_limiter_by_ip = RateLimiter(max_attempts=10, window_seconds=300, lockout_seconds=900)
_login_limiter_by_email = RateLimiter(max_attempts=5, window_seconds=300, lockout_seconds=900)

# BUG FIX: Stricter email validation to prevent invalid formats
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    created_at: float


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# --------------------------------------------------------------------------- #
# User CRUD
# --------------------------------------------------------------------------- #
def get_user_by_email(email: str):
    with db.read_tx() as conn:
        return conn.execute(
            text("SELECT * FROM users WHERE email = :email"),
            {"email": email.lower()},
        ).mappings().fetchone()


def get_user_by_id(user_id: str):
    with db.read_tx() as conn:
        return conn.execute(
            text("SELECT * FROM users WHERE id = :id"),
            {"id": user_id},
        ).mappings().fetchone()


def create_user(email: str, password: str):
    if get_user_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user_id = uuid.uuid4().hex
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active, created_at, updated_at) "
                "VALUES (:id, :email, :password_hash, true, :now, :now)"
            ),
            {"id": user_id, "email": email.lower(), "password_hash": hash_password(password), "now": now},
        )
    return get_user_by_id(user_id)


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
def _user_from_token(token: str):
    try:
        payload = decode_token(token)
    except TokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = get_user_by_id(payload["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account disabled or not found")
    return user


async def get_current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return _user_from_token(token)


async def require_admin(user=Depends(get_current_user)):
    """Gate for server-wide (not per-user) config endpoints — e.g. PUT
    /api/env, which rewrites the shared .env file for every user on this
    deployment. `is_admin` has existed in the users table schema since day
    one but was never actually checked anywhere, so any registered user
    could previously call those endpoints. To make your own account an
    admin: `UPDATE users SET is_admin = true WHERE email = '<your email>';`
    against the PostgreSQL database (see DATABASE_URL in .env)."""
    if not user["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


async def get_user_from_ws(ws: WebSocket):
    """Used by the /ws endpoint: token comes from the query string because
    the browser WebSocket API can't send custom headers."""
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4401)
        return None
    try:
        return _user_from_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post("/register", response_model=TokenOut)
async def register(payload: RegisterRequest):
    user = create_user(payload.email, payload.password)
    token = create_token(user["id"])
    return TokenOut(access_token=token, user=UserOut(id=user["id"], email=user["email"], created_at=user["created_at"]))


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    ip_key = f"ip:{client_ip}"
    email_key = f"email:{payload.email.strip().lower()}"

    for limiter, key in ((_login_limiter_by_ip, ip_key), (_login_limiter_by_email, email_key)):
        remaining = limiter.check(key)
        if remaining is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed login attempts. Try again in {int(remaining) + 1}s.",
            )

    user = get_user_by_email(payload.email)
    if not user or not verify_password(payload.password, user["password_hash"]):
        _login_limiter_by_ip.record_failure(ip_key)
        _login_limiter_by_email.record_failure(email_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account disabled")

    _login_limiter_by_ip.record_success(ip_key)
    _login_limiter_by_email.record_success(email_key)
    token = create_token(user["id"])
    return TokenOut(access_token=token, user=UserOut(id=user["id"], email=user["email"], created_at=user["created_at"]))


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return UserOut(id=user["id"], email=user["email"], created_at=user["created_at"])
