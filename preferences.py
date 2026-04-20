"""
User preferences — Phase 3 of the v2.0 spec (§7, §10).

A user's preferences form a hard-constraint / soft-preference hierarchy
that sits between the user's question and Claude. The CRUD surface is
owner-only (no cross-user visibility); admins read via the admin surface
in admin.py.

The *injection* side — building the system-prompt preference block per
request — lives alongside the chat handler so the prompt assembly is
co-located with the call site. See `build_preferences_block()` below;
chats.py imports it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from db import get_db
from models import Preference, User

router = APIRouter(dependencies=[Depends(auth.get_current_user)])

ALLOWED_KINDS = {"wine", "grape", "region", "style", "ingredient", "other"}
ALLOWED_STRICTNESS = {"strict", "soft"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _pref_dict(p: Preference) -> dict:
    return {
        "id": p.id,
        "user_id": p.user_id,
        "kind": p.kind,
        "value": p.value,
        "strictness": p.strictness,
        "notes": p.notes,
        "created_at": p.created_at,
    }


def _format_pref_line(p: Preference) -> str:
    """One bullet for the injection block, e.g. `- Gewürztraminer (grape)`."""
    label = (p.value or "").strip() or "(unspecified)"
    kind = (p.kind or "other").strip() or "other"
    line = f"- {label} ({kind})"
    if p.notes and p.notes.strip():
        line += f" — {p.notes.strip()}"
    return line


# ---------------------------------------------------------------------------
# Injection helper (imported by chats.py)
# ---------------------------------------------------------------------------


def build_preferences_block(prefs: List[Preference]) -> str:
    """
    Assemble the system-prompt block per §7.

    Returns an empty string when the user has no preferences — spec §11
    step 4: "Zero-preference users see identical behaviour to v1."
    """
    if not prefs:
        return ""

    strict_rows = [p for p in prefs if p.strictness == "strict"]
    soft_rows = [p for p in prefs if p.strictness == "soft"]

    parts: list[str] = ["\n\n---\n\n"]

    if strict_rows:
        parts.append(
            "## USER HARD CONSTRAINTS\n"
            "The user has asked you to NEVER suggest the following, under any "
            "circumstances. If the molecular-preferred answer includes any of "
            "these, propose the next best molecular bridge that does not "
            "include them. Acknowledge the conflict explicitly in your answer.\n"
        )
        for p in strict_rows:
            parts.append(_format_pref_line(p) + "\n")
        parts.append("\n")

    if soft_rows:
        parts.append(
            "## USER SOFT PREFERENCES\n"
            "The user prefers to avoid the following, but allows exceptions "
            "when the molecular match is uniquely strong. When suggesting any "
            "of these, always call this out and explain why the molecular "
            "bridge justifies the exception.\n"
        )
        for p in soft_rows:
            parts.append(_format_pref_line(p) + "\n")
        parts.append("\n")

    parts.append(
        "## RANKING RULE (non-negotiable)\n"
        "Your ranking hierarchy for every pairing suggestion is:\n"
        "1. Hard constraints: filter entirely.\n"
        "2. Molecular-bridge method (Chartier): primary ranking of remaining "
        "options.\n"
        "3. Soft preferences: re-rank within molecular ties. Never demote a "
        "top molecular match out of the visible list — surface it with a note "
        "instead.\n"
        "4. Learned patterns (from v2.1 onward): re-rank within soft-"
        "preference ties only.\n"
    )

    return "".join(parts)


async def load_preferences_for(db: AsyncSession, user_id: str) -> List[Preference]:
    result = await db.execute(
        select(Preference)
        .where(Preference.user_id == user_id)
        .order_by(Preference.created_at.asc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class CreatePreferenceBody(BaseModel):
    kind: str = Field(..., description="wine | grape | region | style | ingredient | other")
    value: str
    strictness: str = Field(..., description="'strict' or 'soft'")
    notes: Optional[str] = None


class PatchPreferenceBody(BaseModel):
    kind: Optional[str] = None
    value: Optional[str] = None
    strictness: Optional[str] = None
    notes: Optional[str] = None


def _validate_create(body: CreatePreferenceBody) -> None:
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind; must be one of {sorted(ALLOWED_KINDS)}")
    if body.strictness not in ALLOWED_STRICTNESS:
        raise HTTPException(
            status_code=400,
            detail="Invalid strictness; must be 'strict' or 'soft'",
        )
    if not body.value or not body.value.strip():
        raise HTTPException(status_code=400, detail="Value is required")


@router.get("/api/preferences")
async def list_preferences(
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = await load_preferences_for(db, user.id)
    return {"preferences": [_pref_dict(p) for p in prefs]}


@router.post("/api/preferences")
async def create_preference(
    body: CreatePreferenceBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_create(body)
    pref = Preference(
        id=str(uuid.uuid4()),
        user_id=user.id,
        kind=body.kind,
        value=body.value.strip(),
        strictness=body.strictness,
        notes=(body.notes.strip() if body.notes else None),
        created_at=_iso(_now()),
    )
    db.add(pref)
    await db.commit()
    return _pref_dict(pref)


@router.patch("/api/preferences/{pref_id}")
async def update_preference(
    pref_id: str,
    body: PatchPreferenceBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pref = await db.get(Preference, pref_id)
    if pref is None:
        raise HTTPException(status_code=404, detail="Preference not found")
    if pref.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your preference")

    if body.kind is not None:
        if body.kind not in ALLOWED_KINDS:
            raise HTTPException(status_code=400, detail="Invalid kind")
        pref.kind = body.kind
    if body.value is not None:
        v = body.value.strip()
        if not v:
            raise HTTPException(status_code=400, detail="Value cannot be empty")
        pref.value = v
    if body.strictness is not None:
        if body.strictness not in ALLOWED_STRICTNESS:
            raise HTTPException(status_code=400, detail="Invalid strictness")
        pref.strictness = body.strictness
    if body.notes is not None:
        pref.notes = body.notes.strip() or None

    await db.commit()
    return _pref_dict(pref)


@router.delete("/api/preferences/{pref_id}")
async def delete_preference(
    pref_id: str,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pref = await db.get(Preference, pref_id)
    if pref is None:
        raise HTTPException(status_code=404, detail="Preference not found")
    if pref.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your preference")
    await db.delete(pref)
    await db.commit()
    return {"ok": True}
