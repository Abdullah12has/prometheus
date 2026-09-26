"""Single-operator cookie auth (ADMIN_PASSWORD) with origin and CSRF-token checks."""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import get_db
from .errors import ApiError
from .models import AuthSession, utcnow

COOKIE = "permetheus_session"
SESSION_TTL = timedelta(hours=12)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_FAILURES, FAILURE_WINDOW = 5, 300.0

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_origin(request: Request) -> None:
    # Browsers always send Origin on cross-site unsafe requests; non-browser clients may omit it.
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in request.app.state.settings.allowed_origins:
        raise ApiError(403, "origin_rejected", "Request origin is not allowed")


def require_session(request: Request, db: Session = Depends(get_db)) -> AuthSession:
    token = request.cookies.get(COOKIE)
    session = token and db.scalar(
        select(AuthSession).where(AuthSession.token_hash == _hash(token), AuthSession.expires_at > utcnow())
    )
    if not session:
        raise ApiError(401, "unauthenticated", "Sign in required")
    if request.method in UNSAFE_METHODS:
        check_origin(request)
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent.encode(), session.csrf_token.encode()):
            raise ApiError(403, "csrf_failed", "Missing or invalid X-CSRF-Token header")
    return session


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SessionOut(BaseModel):
    authenticated: bool
    csrf_token: str | None = None
    expires_at: datetime | None = None


def _throttle(request: Request) -> list[float]:
    # ponytail: in-process per-IP limiter, fine for one local API process.
    failures = request.app.state.login_failures
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [t for t in failures.get(key, []) if now - t < FAILURE_WINDOW]
    failures[key] = recent
    if len(recent) >= MAX_FAILURES:
        raise ApiError(429, "rate_limited", "Too many failed sign-in attempts; try again later")
    return recent


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    check_origin(request)
    configured = request.app.state.settings.admin_password
    if configured is None:
        raise ApiError(503, "auth_not_configured", "ADMIN_PASSWORD is not set in .env")
    recent = _throttle(request)
    if not hmac.compare_digest(body.password.encode(), configured.get_secret_value().encode()):
        recent.append(time.monotonic())
        raise ApiError(401, "invalid_credentials", "Incorrect password")

    token = secrets.token_urlsafe(32)
    session = AuthSession(token_hash=_hash(token), csrf_token=secrets.token_urlsafe(32), expires_at=utcnow() + SESSION_TTL)
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= utcnow()))
    db.add(session)
    db.commit()
    response.set_cookie(
        COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="strict",
        secure=request.app.state.settings.cookie_secure, path="/api",
    )
    return SessionOut(authenticated=True, csrf_token=session.csrf_token, expires_at=session.expires_at)


@router.get("/me", response_model=SessionOut)
def me(session: AuthSession = Depends(require_session)):
    return SessionOut(authenticated=True, csrf_token=session.csrf_token, expires_at=session.expires_at)


@router.post("/logout", response_model=SessionOut)
def logout(response: Response, session: AuthSession = Depends(require_session), db: Session = Depends(get_db)):
    db.execute(delete(AuthSession).where(AuthSession.id == session.id))
    db.commit()
    response.delete_cookie(COOKIE, path="/api")
    return SessionOut(authenticated=False)
