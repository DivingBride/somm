"""
Magic-link authentication, session cookies, and Resend email.

Phase 0 of the v2.0 spec (§5, §10). Implements:
  - Token generation (32-byte URL-safe base64, SHA-256 hashed at rest).
  - Magic-link request/consume flow with 15-minute expiry, one-time use.
  - Signed session cookies (itsdangerous) backed by a sessions row in DB.
  - 30-day rolling session expiry, refreshed on each authenticated request.
  - Invite acceptance (creates user, issues magic link).
  - FastAPI dependency `get_current_user` for gating routes.
  - Minimal audit_log writes on login/invite events.

All email goes through Resend (§13 decision 1; Postmark is the documented
fallback).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import resend
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from models import AuditLog, Invite, MagicLinkToken, Session, User

log = logging.getLogger("somm.auth")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Somm <onboarding@resend.dev>")
APP_URL = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")

# In prod (HTTPS) this MUST be true; set COOKIE_SECURE=false for local dev.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"

COOKIE_NAME = "somm_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30        # 30 days
MAGIC_LINK_TTL = timedelta(minutes=15)     # §5 — 15-minute expiry
SESSION_TTL = timedelta(days=30)           # §5 — 30-day rolling

_signer: Optional[URLSafeSerializer] = None


def _get_signer() -> URLSafeSerializer:
    """Lazy-init the cookie signer so missing SESSION_SECRET fails loudly."""
    global _signer
    if _signer is None:
        if not SESSION_SECRET:
            print(
                "[somm.auth] FATAL: SESSION_SECRET env var is required for cookie signing.",
                file=sys.stderr,
            )
            raise RuntimeError("SESSION_SECRET is not set")
        _signer = URLSafeSerializer(SESSION_SECRET, salt="somm-session-cookie")
    return _signer


if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _new_raw_token(n_bytes: int = 32) -> str:
    """URL-safe random token. Never stored at rest — only its hash."""
    return secrets.token_urlsafe(n_bytes)


def _hash_token(raw: str) -> str:
    """SHA-256 hex digest of the raw token."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _log_audit(
    db: AsyncSession,
    actor_user_id: Optional[str],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            created_at=_iso(_now()),
            metadata_json=json.dumps(metadata) if metadata else None,
        )
    )


# ---------------------------------------------------------------------------
# Email (Resend)
# ---------------------------------------------------------------------------


def _send_magic_link_email(to_email: str, link: str) -> None:
    """
    Send the magic-link email. Swallows failures (we log them) so that the
    API layer can still return the generic 200 response without leaking which
    addresses are registered. Deliverability monitoring is a §13 risk we
    handle in Phase 5 hardening.
    """
    if not RESEND_API_KEY:
        # Dev mode: print to stdout so the founder can copy the link locally.
        print(f"[somm.auth] (dev) Magic link for {to_email}: {link}")
        return

    subject = "Your Somm login link"
    html = f"""\
<p>Hi,</p>
<p>Click the link below to sign in to Somm. The link expires in 15 minutes
and can only be used once.</p>
<p><a href="{link}">Sign in to Somm</a></p>
<p>If you did not request this, you can safely ignore this email.</p>
"""
    text = (
        f"Sign in to Somm: {link}\n\n"
        "This link expires in 15 minutes and can only be used once.\n"
        "If you did not request this, you can safely ignore this email.\n"
    )
    try:
        resend.Emails.send(
            {
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": subject,
                "html": html,
                "text": text,
            }
        )
    except Exception as e:  # noqa: BLE001
        log.error("Resend send failed for %s: %s", to_email, e)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


async def _create_session(
    db: AsyncSession, user_id: str, request: Request
) -> str:
    """
    Create a new session row and return the *raw* session token to be stored
    in the client cookie (after signing).
    """
    raw = _new_raw_token()
    now = _now()
    db.add(
        Session(
            id=str(uuid.uuid4()),
            user_id=user_id,
            session_token_hash=_hash_token(raw),
            created_at=_iso(now),
            expires_at=_iso(now + SESSION_TTL),
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    )
    return raw


def _set_session_cookie(response: Response, raw_token: str) -> None:
    signed = _get_signer().dumps(raw_token)
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


async def _lookup_session(db: AsyncSession, raw_token: str) -> Optional[Session]:
    token_hash = _hash_token(raw_token)
    result = await db.execute(
        select(Session).where(Session.session_token_hash == token_hash)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    response: Response,
    somm_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Resolve the current user from the signed session cookie.

    Side effect: refreshes the session's expires_at (30-day rolling) and the
    cookie Max-Age, per §5.
    Raises 401 if missing/invalid/expired/user revoked.
    """
    if not somm_session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        raw_token = _get_signer().loads(somm_session)
    except BadSignature:
        _clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid session")

    session = await _lookup_session(db, raw_token)
    if session is None:
        _clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Session not found")

    expires = _parse_iso(session.expires_at)
    if expires is None or expires < _now():
        await db.delete(session)
        await db.commit()
        _clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Session expired")

    user = await db.get(User, session.user_id)
    if user is None or user.status != "active":
        # Revoked users: drop all their sessions on next request (§6).
        await db.delete(session)
        await db.commit()
        _clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="User not active")

    # Rolling refresh.
    new_expiry = _now() + SESSION_TTL
    session.expires_at = _iso(new_expiry)
    await db.commit()
    _set_session_cookie(response, raw_token)

    # Stash on request.state for downstream handlers that want it.
    request.state.user = user
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

router = APIRouter()


class MagicLinkRequest(BaseModel):
    email: EmailStr


class AcceptInviteRequest(BaseModel):
    invite_token: str
    email: EmailStr
    display_name: Optional[str] = None


@router.post("/api/auth/magic-link")
async def request_magic_link(
    body: MagicLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Always 200 with a generic message (§5 — no account enumeration).
    If the email matches an active user, a magic link is sent.
    """
    email = body.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is not None and user.status == "active":
        raw = _new_raw_token()
        now = _now()
        db.add(
            MagicLinkToken(
                id=str(uuid.uuid4()),
                user_id=user.id,
                token_hash=_hash_token(raw),
                created_at=_iso(now),
                expires_at=_iso(now + MAGIC_LINK_TTL),
            )
        )
        await db.commit()
        link = f"{APP_URL}/auth/callback?token={raw}"
        _send_magic_link_email(email, link)

    return {
        "ok": True,
        "message": "If that email is registered, we've sent a sign-in link.",
    }


@router.get("/auth/callback")
async def magic_link_callback(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate the magic link, mark it used, start a session, set the cookie,
    and redirect home.
    """
    token_hash = _hash_token(token)
    result = await db.execute(
        select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash)
    )
    mlt = result.scalar_one_or_none()

    if mlt is None or mlt.used_at is not None:
        raise HTTPException(status_code=400, detail="Invalid or already-used link")

    expires = _parse_iso(mlt.expires_at)
    if expires is None or expires < _now():
        raise HTTPException(status_code=400, detail="Link has expired")

    user = await db.get(User, mlt.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=400, detail="Account not active")

    mlt.used_at = _iso(_now())
    user.last_login_at = _iso(_now())

    raw_session = await _create_session(db, user.id, request)
    await _log_audit(db, user.id, "login", target_type="user", target_id=user.id)
    await db.commit()

    redirect = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(redirect, raw_session)
    return redirect


@router.post("/api/auth/logout")
async def logout(
    response: Response,
    somm_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
):
    if somm_session:
        try:
            raw_token = _get_signer().loads(somm_session)
            session = await _lookup_session(db, raw_token)
            if session is not None:
                await db.delete(session)
                await db.commit()
        except BadSignature:
            pass
    _clear_session_cookie(response)
    return {"ok": True}


@router.post("/api/auth/accept-invite")
async def accept_invite(
    body: AcceptInviteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate the invite, create a new user, mark the invite used, and send
    a magic-link email so the user can complete sign-in.
    """
    result = await db.execute(
        select(Invite).where(Invite.token == body.invite_token)
    )
    invite = result.scalar_one_or_none()
    if invite is None or invite.used_at is not None:
        raise HTTPException(status_code=400, detail="Invalid or already-used invite")

    expires = _parse_iso(invite.expires_at)
    if expires is not None and expires < _now():
        raise HTTPException(status_code=400, detail="Invite has expired")

    email = body.email.lower().strip()

    # If the invite was pinned to a specific email, enforce it.
    if invite.email and invite.email.lower().strip() != email:
        raise HTTPException(status_code=400, detail="This invite is for a different email")

    # Reject collision with an existing user — one address, one account.
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="An account with that email already exists")

    now = _now()
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=body.display_name,
        role="user",
        status="active",
        created_at=_iso(now),
    )
    db.add(user)

    invite.used_at = _iso(now)
    invite.used_by_user_id = user.id

    # Issue a magic-link immediately so the new user's first action is a normal sign-in.
    raw = _new_raw_token()
    db.add(
        MagicLinkToken(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=_hash_token(raw),
            created_at=_iso(now),
            expires_at=_iso(now + MAGIC_LINK_TTL),
        )
    )

    await _log_audit(
        db,
        actor_user_id=user.id,
        action="invite_accepted",
        target_type="invite",
        target_id=invite.id,
        metadata={"email": email},
    )
    await db.commit()

    _send_magic_link_email(email, f"{APP_URL}/auth/callback?token={raw}")
    return {
        "ok": True,
        "message": "Account created. Check your email for a sign-in link.",
    }


@router.get("/api/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }
