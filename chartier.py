"""
Chartier library read API — Phase 4 of the v2.0 spec (§8, §10).

All endpoints require a signed-in user (any role) — the library is a
shared resource, not personal data. Admin-only mutation sits in admin.py.
"""

from __future__ import annotations

import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from db import get_db
from models import ChartierEntry, User

router = APIRouter(dependencies=[Depends(auth.get_current_user)])


# ---------------------------------------------------------------------------
# Molecule-family parsing
# ---------------------------------------------------------------------------

# Heuristic buckets — parsed from the molecule_logic column on the fly.
# Each family maps to one or more case-insensitive substrings. The first
# match wins; rows with no match fall into "other".
#
# These buckets are the ones the library UI surfaces as faceted filters.
# Refining the list is a one-line change here; it does not require a
# schema migration.
MOLECULE_FAMILIES: list[tuple[str, list[str]]] = [
    ("rose / linalool", ["linalool", "rose", "geraniol"]),
    ("anise / estragole", ["anethole", "estragole", "anise", "fennel"]),
    ("sotolon", ["sotolon"]),
    ("rotundone / pepper", ["rotundone", "hotrienol"]),
    ("whisky-lactone / oak", ["whisky lactone", "whisky-lactone", "oak lactone", "lactone"]),
    ("vanillin / oak", ["vanillin", "vanilla"]),
    ("pyrazines / green", ["pyrazine", "ibmp"]),
    ("sulfur / thiol", ["thiol", "mercaptan", "sulfur"]),
    ("terpenes", ["terpene", "limonene", "myrcene"]),
    ("esters / fruit", ["ester", "fruit ester", "isoamyl"]),
    ("eugenol / clove", ["eugenol", "clove"]),
]


def molecule_family_for(logic: Optional[str]) -> str:
    if not logic:
        return "other"
    low = logic.lower()
    for name, needles in MOLECULE_FAMILIES:
        for n in needles:
            if n in low:
                return name
    return "other"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def entry_dict(e: ChartierEntry) -> dict:
    return {
        "id": e.id,
        "wine_style": e.wine_style,
        "dish": e.dish,
        "molecule_logic": e.molecule_logic,
        "wine_examples": e.wine_examples,
        "molecule_family": molecule_family_for(e.molecule_logic),
        "imported_at": e.imported_at,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/chartier")
async def list_entries(
    q: Optional[str] = Query(None, description="Free-text search across all columns"),
    wine_style: Optional[str] = Query(None, description="Exact-prefix match on wine_style"),
    molecule_family: Optional[str] = Query(None, description="Filter by parsed family bucket"),
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ChartierEntry).order_by(ChartierEntry.id.asc())

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                ChartierEntry.wine_style.ilike(like),
                ChartierEntry.dish.ilike(like),
                ChartierEntry.molecule_logic.ilike(like),
                ChartierEntry.wine_examples.ilike(like),
            )
        )
    if wine_style:
        stmt = stmt.where(ChartierEntry.wine_style.ilike(f"{wine_style}%"))

    result = await db.execute(stmt)
    entries = list(result.scalars().all())

    # Molecule-family filtering happens in Python (derived column; no SQL
    # representation). We also surface the full family set so the UI can
    # render faceted filters without a second round-trip.
    if molecule_family:
        target = molecule_family.lower()
        entries = [e for e in entries if molecule_family_for(e.molecule_logic).lower() == target]

    all_families = sorted({molecule_family_for(e.molecule_logic) for e in entries})
    all_styles = sorted({
        (e.wine_style or "").split("\n", 1)[0].split("(", 1)[0].strip()
        for e in entries
        if e.wine_style
    })

    return {
        "entries": [entry_dict(e) for e in entries],
        "total": len(entries),
        "facets": {
            "molecule_families": all_families,
            "wine_styles": [s for s in all_styles if s],
        },
    }


@router.get("/api/chartier/{entry_id}")
async def get_entry(
    entry_id: int,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = await db.get(ChartierEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Chartier entry not found")
    return entry_dict(entry)
