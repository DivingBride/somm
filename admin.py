"""
Admin panel API.

All endpoints are gated by auth.get_current_admin and write audit_log
entries for anything that mutates state (§6 admin actions, §4 audit_log).

Two deltas from §12 Phase 1 scope, added for the founder's Resend-free
5-user circle:
  - POST /api/admin/users      → create a user directly (skips the invite
                                 round-trip; admin already trusts them).
  - POST /api/admin/users/{id}/magic-link
                               → generate a fresh sign-in link the admin
                                 copies/pastes to the user via their own
                                 channel. Doubles as a break-glass if a
                                 user ever gets locked out.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
import chartier_sync
from db import get_db
from models import AuditLog, Chat, Invite, MagicLinkToken, User

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.get_current_admin)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _audit(
    db: AsyncSession,
    actor: User,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            actor_user_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            created_at=_iso(_now()),
            metadata_json=json.dumps(metadata) if metadata else None,
        )
    )


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    }


def _invite_dict(i: Invite) -> dict:
    return {
        "id": i.id,
        "token": i.token,
        "email": i.email,
        "created_by": i.created_by,
        "expires_at": i.expires_at,
        "used_at": i.used_at,
        "used_by_user_id": i.used_by_user_id,
    }


def _audit_dict(a: AuditLog) -> dict:
    return {
        "id": a.id,
        "actor_user_id": a.actor_user_id,
        "action": a.action,
        "target_type": a.target_type,
        "target_id": a.target_id,
        "created_at": a.created_at,
        "metadata_json": a.metadata_json,
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class CreateUserBody(BaseModel):
    email: EmailStr
    display_name: Optional[str] = None
    role: Optional[str] = "user"  # 'user' | 'admin'


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return {"users": [_user_dict(u) for u in result.scalars().all()]}


@router.post("/users")
async def create_user(
    body: CreateUserBody,
    admin: User = Depends(auth.get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    email = body.email.lower().strip()

    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="A user with that email already exists")

    role = body.role if body.role in ("user", "admin") else "user"

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=body.display_name or email.split("@")[0],
        role=role,
        status="active",
        created_at=_iso(_now()),
    )
    db.add(user)
    await _audit(
        db, admin, "user_created",
        target_type="user", target_id=user.id,
        metadata={"email": email, "role": role},
    )
    await db.commit()
    return _user_dict(user)


@router.post("/users/{user_id}/revoke")
async def revoke_user(
    user_id: str,
    admin: User = Depends(auth.get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot revoke yourself")

    user.status = "revoked"
    await _audit(
        db, admin, "user_revoked",
        target_type="user", target_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()
    return _user_dict(user)


@router.post("/users/{user_id}/magic-link")
async def generate_magic_link_for_user(
    user_id: str,
    admin: User = Depends(auth.get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a fresh 15-minute one-time sign-in link for an active user.
    Returns the raw URL so the admin can paste it into iMessage/WhatsApp.

    This is the Resend-free equivalent of /api/auth/magic-link — same
    token mechanics, same expiry, just surfaced via the admin panel.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status != "active":
        raise HTTPException(status_code=400, detail="User is not active")

    raw = auth._new_raw_token()
    now = _now()
    db.add(
        MagicLinkToken(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=auth._hash_token(raw),
            created_at=_iso(now),
            expires_at=_iso(now + auth.MAGIC_LINK_TTL),
        )
    )
    await _audit(
        db, admin, "admin_magic_link_issued",
        target_type="user", target_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()

    link = f"{auth.APP_URL}/auth/callback?token={raw}"
    return {
        "url": link,
        "expires_in_minutes": int(auth.MAGIC_LINK_TTL.total_seconds() // 60),
        "user": _user_dict(user),
    }


@router.get("/users/{user_id}/chats")
async def list_user_chats(
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin read-only view of a user's chats (§10). Returns both archived and
    active chats so admins can spot deletions. Newest first.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    result = await db.execute(
        select(Chat)
        .where(Chat.user_id == user_id)
        .order_by(Chat.updated_at.desc().nullslast(), Chat.created_at.desc())
    )
    chats = result.scalars().all()
    return {
        "user": _user_dict(user),
        "chats": [
            {
                "id": c.id,
                "user_id": c.user_id,
                "title": c.title,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
                "archived_at": c.archived_at,
            }
            for c in chats
        ],
    }


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class CreateInviteBody(BaseModel):
    email: Optional[EmailStr] = None        # if set, pins the invite
    expires_in_days: Optional[int] = 7


@router.get("/invites")
async def list_invites(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Invite).order_by(Invite.expires_at.desc()))
    invites = result.scalars().all()
    return {"invites": [_invite_dict(i) for i in invites]}


@router.post("/invites")
async def create_invite(
    body: CreateInviteBody,
    admin: User = Depends(auth.get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    days = body.expires_in_days if (body.expires_in_days and body.expires_in_days > 0) else 7
    raw = auth._new_raw_token()
    now = _now()
    invite = Invite(
        id=str(uuid.uuid4()),
        token=raw,
        email=(body.email.lower().strip() if body.email else None),
        created_by=admin.id,
        expires_at=_iso(now + timedelta(days=days)),
    )
    db.add(invite)
    await _audit(
        db, admin, "invite_created",
        target_type="invite", target_id=invite.id,
        metadata={"email": invite.email, "expires_in_days": days},
    )
    await db.commit()

    # Convenience: expose the shareable URL alongside the stored row.
    return {
        **_invite_dict(invite),
        "share_url": f"{auth.APP_URL}/invite/{raw}",
    }


@router.delete("/invites/{invite_id}")
async def revoke_invite(
    invite_id: str,
    admin: User = Depends(auth.get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    invite = await db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.used_at is not None:
        raise HTTPException(status_code=400, detail="Invite has already been used")

    await db.delete(invite)
    await _audit(
        db, admin, "invite_revoked",
        target_type="invite", target_id=invite.id,
    )
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chartier library sync
# ---------------------------------------------------------------------------


@router.post("/chartier/sync")
async def chartier_sync_endpoint(
    admin: User = Depends(auth.get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-triggered re-ingestion of the Chartier xlsx (§8). Idempotent:
    a second call with an unchanged spreadsheet is a no-op (everything
    shows up as `unchanged`). The sync function writes its own audit_log
    entry, so we don't duplicate here.
    """
    try:
        summary = await chartier_sync.sync_chartier_library(
            actor_user_id=admin.id, db=db
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, **summary}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit")
async def list_audit(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: Optional[str] = None,
    actor_user_id: Optional[str] = None,
):
    stmt = select(AuditLog).order_by(desc(AuditLog.created_at))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    stmt = stmt.limit(limit).offset(offset)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    count_stmt = select(func.count()).select_from(AuditLog)
    if action:
        count_stmt = count_stmt.where(AuditLog.action == action)
    if actor_user_id:
        count_stmt = count_stmt.where(AuditLog.actor_user_id == actor_user_id)
    total = (await db.execute(count_stmt)).scalar_one()

    return {
        "events": [_audit_dict(a) for a in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
