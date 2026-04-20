"""
Saved pairings API — Phase 4 of the v2.0 spec (§9, §10).

Each row captures a pairing the user wants to keep. Three sources:
  - 'chat'     → copied from a Claude answer inside a chat (source_ref = chats.id)
  - 'chartier' → copied from the Chartier library (source_ref = chartier_library.id)
  - 'custom'   → user-entered from /saved/new

All rows are private in v2.0 (is_private=True). The `tried_at` column
ships from day one so v2.1 ratings can attach without a migration
(§13 decision 4). All CRUD is owner-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from db import get_db
from models import ChartierEntry, SavedPairing, User

router = APIRouter(dependencies=[Depends(auth.get_current_user)])

ALLOWED_SOURCES = {"chat", "chartier", "custom"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _saved_dict(s: SavedPairing) -> dict:
    return {
        "id": s.id,
        "user_id": s.user_id,
        "source": s.source,
        "source_ref": s.source_ref,
        "wine": s.wine,
        "dish": s.dish,
        "molecule_logic": s.molecule_logic,
        "wine_examples": s.wine_examples,
        "user_notes": s.user_notes,
        "is_private": s.is_private,
        "tried_at": s.tried_at,
        "created_at": s.created_at,
    }


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CreateSavedBody(BaseModel):
    source: str = Field(..., description="chat | chartier | custom")
    source_ref: Optional[str] = None
    wine: Optional[str] = None
    dish: Optional[str] = None
    molecule_logic: Optional[str] = None
    wine_examples: Optional[str] = None
    user_notes: Optional[str] = None
    # If source='chartier' and source_ref is set, we fill wine/dish/logic/
    # examples from the Chartier row server-side so the snapshot survives
    # an admin re-sync that mutates the library.


class PatchSavedBody(BaseModel):
    user_notes: Optional[str] = None
    tried_at: Optional[str] = None  # ISO timestamp or empty string to clear


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/saved")
async def list_saved(
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SavedPairing)
        .where(SavedPairing.user_id == user.id)
        .order_by(SavedPairing.created_at.desc())
    )
    return {"saved": [_saved_dict(s) for s in result.scalars().all()]}


@router.post("/api/saved")
async def create_saved(
    body: CreateSavedBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.source not in ALLOWED_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source; must be one of {sorted(ALLOWED_SOURCES)}",
        )

    wine = (body.wine or "").strip() or None
    dish = (body.dish or "").strip() or None
    molecule_logic = (body.molecule_logic or "").strip() or None
    wine_examples = (body.wine_examples or "").strip() or None
    source_ref = (body.source_ref or "").strip() or None

    # Chartier-sourced rows snapshot the library row by id so later
    # re-syncs cannot rewrite the user's saved copy.
    if body.source == "chartier":
        if not source_ref:
            raise HTTPException(
                status_code=400,
                detail="chartier source requires source_ref",
            )
        try:
            entry_id = int(source_ref)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="chartier source_ref must be an int")
        entry = await db.get(ChartierEntry, entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Chartier entry not found")
        wine = wine or entry.wine_style
        dish = dish or entry.dish
        molecule_logic = molecule_logic or entry.molecule_logic
        wine_examples = wine_examples or entry.wine_examples

    # Custom requires at least a wine + dish — guardrail for empty rows.
    if body.source == "custom":
        if not wine or not dish:
            raise HTTPException(
                status_code=400,
                detail="custom source requires both wine and dish",
            )

    # Chat-sourced rows just snapshot whatever the UI extracted from the
    # Claude answer; we don't validate against chats.messages.id because
    # messages can be long and the fields might legitimately be empty.

    pairing = SavedPairing(
        id=str(uuid.uuid4()),
        user_id=user.id,
        source=body.source,
        source_ref=source_ref,
        wine=wine,
        dish=dish,
        molecule_logic=molecule_logic,
        wine_examples=wine_examples,
        user_notes=(body.user_notes.strip() if body.user_notes else None) or None,
        is_private=True,
        tried_at=None,
        created_at=_iso(_now()),
    )
    db.add(pairing)
    await db.commit()
    return _saved_dict(pairing)


@router.patch("/api/saved/{pairing_id}")
async def update_saved(
    pairing_id: str,
    body: PatchSavedBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pairing = await db.get(SavedPairing, pairing_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="Saved pairing not found")
    if pairing.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your saved pairing")

    if body.user_notes is not None:
        pairing.user_notes = body.user_notes.strip() or None
    if body.tried_at is not None:
        # Empty string clears the flag; otherwise stamp verbatim.
        pairing.tried_at = body.tried_at.strip() or None

    await db.commit()
    return _saved_dict(pairing)


@router.delete("/api/saved/{pairing_id}")
async def delete_saved(
    pairing_id: str,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pairing = await db.get(SavedPairing, pairing_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="Saved pairing not found")
    if pairing.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your saved pairing")
    await db.delete(pairing)
    await db.commit()
    return {"ok": True}
