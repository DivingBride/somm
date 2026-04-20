"""
Chartier library ingestion — Phase 4 of the v2.0 spec (§8).

Reads `Chartier_Wine_Food_Pairings.xlsx` from CHARTIER_XLSX_PATH, normalises
each row, computes a per-row SHA-256 checksum, and upserts into the
`chartier_library` table keyed by row id (1-based xlsx row order).

Ingestion runs:
  - On application startup (wired from main.py via the existing @app.on_event),
  - On admin request via POST /api/admin/chartier/sync.

The spreadsheet is the single source of truth (§8 "Read-only guarantee"):
we never write to the xlsx from the app, and the `chartier_library` table
is considered a read-through cache, rebuilt from the file on demand.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import async_session_factory
from models import AuditLog, ChartierEntry

log = logging.getLogger("somm.chartier")

# Column layout of Chartier_Wine_Food_Pairings.xlsx (inspected 2026-04-19):
#   Row 1  — title banner
#   Row 2  — column headers
#   Row 3+ — data rows
# 4 columns: wine_style, dish, molecule_logic, wine_examples.
HEADER_ROWS = 2
COLUMNS = ("wine_style", "dish", "molecule_logic", "wine_examples")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def xlsx_path() -> Path:
    """
    Resolve the Chartier xlsx location. Defaults to the sibling of the
    somm-app/ directory where the project ships with the spreadsheet;
    overridable via CHARTIER_XLSX_PATH for dev/prod.
    """
    override = os.environ.get("CHARTIER_XLSX_PATH")
    if override:
        return Path(override).expanduser().resolve()
    # Default: ../Chartier_Wine_Food_Pairings.xlsx relative to this file.
    return (Path(__file__).parent.parent / "Chartier_Wine_Food_Pairings.xlsx").resolve()


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise_cell(raw) -> Optional[str]:
    """Strip whitespace and collapse multi-line cells to single spaces."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Collapse line breaks and runs of whitespace.
    return " ".join(text.split())


def _row_checksum(fields: dict) -> str:
    """Stable SHA-256 over the normalised fields, sorted-keys JSON."""
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def _read_rows_from_xlsx(path: Path) -> list[dict]:
    """
    Open the xlsx and return a list of dicts with keys matching COLUMNS and
    an `id` field set to the 1-based xlsx row number of the source row.
    The caller handles DB upsert + audit.
    """
    import openpyxl  # late import; only needed during sync

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows: list[dict] = []
    for idx, raw_row in enumerate(ws.iter_rows(values_only=True), start=1):
        if idx <= HEADER_ROWS:
            continue
        values = [_normalise_cell(c) for c in (raw_row[: len(COLUMNS)])]
        # Skip entirely blank rows — the xlsx sometimes has trailing empties.
        if not any(values):
            continue
        row = dict(zip(COLUMNS, values))
        row["id"] = idx
        rows.append(row)
    wb.close()
    return rows


async def sync_chartier_library(
    *, actor_user_id: Optional[str] = None, db: Optional[AsyncSession] = None
) -> dict:
    """
    Upsert the Chartier spreadsheet into chartier_library.

    Returns a summary dict: {"total": N, "added": a, "updated": u,
    "unchanged": c, "removed": r, "path": str}.

    If `db` is provided, uses that session (so admin-triggered syncs share
    the request transaction). Otherwise opens its own.
    """
    path = xlsx_path()
    if not path.exists():
        raise FileNotFoundError(f"Chartier xlsx not found at {path}")

    rows = _read_rows_from_xlsx(path)
    now_iso = _iso(_now())

    owns_session = db is None
    if owns_session:
        db = async_session_factory()

    try:
        # Snapshot existing rows keyed by id for delta computation.
        existing_map: dict[int, ChartierEntry] = {}
        result = await db.execute(select(ChartierEntry))
        for row in result.scalars().all():
            existing_map[row.id] = row

        added = 0
        updated = 0
        unchanged = 0
        seen_ids: set[int] = set()

        for row in rows:
            row_id = row["id"]
            seen_ids.add(row_id)
            fields = {k: row.get(k) for k in COLUMNS}
            checksum = _row_checksum(fields)

            existing = existing_map.get(row_id)
            if existing is None:
                db.add(
                    ChartierEntry(
                        id=row_id,
                        **fields,
                        imported_at=now_iso,
                        checksum=checksum,
                    )
                )
                added += 1
            elif existing.checksum != checksum:
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.imported_at = now_iso
                existing.checksum = checksum
                updated += 1
            else:
                unchanged += 1

        # Remove rows that no longer exist in the spreadsheet.
        removed = 0
        for rid, row in existing_map.items():
            if rid not in seen_ids:
                await db.delete(row)
                removed += 1

        summary = {
            "total": len(rows),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "removed": removed,
            "path": str(path),
        }

        db.add(
            AuditLog(
                id=str(uuid.uuid4()),
                actor_user_id=actor_user_id,
                action="chartier_synced",
                target_type="chartier_library",
                target_id=None,
                created_at=now_iso,
                metadata_json=json.dumps(summary),
            )
        )
        await db.commit()
        log.info("chartier sync: %s", summary)
        return summary
    except Exception:
        if owns_session:
            await db.rollback()
        raise
    finally:
        if owns_session:
            await db.close()


async def sync_on_startup() -> None:
    """
    Startup entry point. Swallows exceptions so a misconfigured path doesn't
    block the app from booting — the admin surface can trigger a retry once
    the path is fixed.
    """
    try:
        summary = await sync_chartier_library()
        print(f"[somm.chartier] startup sync: {summary}")
    except FileNotFoundError as e:
        print(f"[somm.chartier] startup sync skipped: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[somm.chartier] startup sync failed: {e}", file=sys.stderr)
