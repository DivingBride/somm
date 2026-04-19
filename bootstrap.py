"""
One-shot bootstrap actions that run on application startup.

Keeps startup logic out of main.py so it's easy to unit-test and extend.
"""

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from db import async_session_factory
from models import AuditLog, User

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_DISPLAY_NAME = os.environ.get("ADMIN_DISPLAY_NAME", "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def seed_admin_if_needed() -> None:
    """
    Create the founder admin account on first boot if ADMIN_EMAIL is set.

    Idempotent: if a user with that email already exists with role='admin',
    we're fine. If they exist with a non-admin role, we log a warning and
    refuse to silently promote — promotions go through the admin panel.

    First sign-in: the admin hits /login, submits their email, and the
    magic link is printed to stdout (since RESEND_API_KEY is unset in the
    founder's chosen config). They copy it from Render logs.
    """
    if not ADMIN_EMAIL:
        return

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.email == ADMIN_EMAIL))
        existing = result.scalar_one_or_none()

        if existing is not None:
            if existing.role != "admin":
                print(
                    f"[somm.bootstrap] WARNING: user {ADMIN_EMAIL} exists with role "
                    f"'{existing.role}' — not promoting automatically.",
                )
            return

        admin = User(
            id=str(uuid.uuid4()),
            email=ADMIN_EMAIL,
            display_name=ADMIN_DISPLAY_NAME or ADMIN_EMAIL.split("@")[0],
            role="admin",
            status="active",
            created_at=_now_iso(),
        )
        db.add(admin)
        db.add(
            AuditLog(
                id=str(uuid.uuid4()),
                actor_user_id=None,
                action="admin_seeded",
                target_type="user",
                target_id=admin.id,
                created_at=_now_iso(),
                metadata_json=f'{{"email": "{ADMIN_EMAIL}"}}',
            )
        )
        await db.commit()
        print(
            f"[somm.bootstrap] Seeded founder admin account: {ADMIN_EMAIL} "
            f"(display name: {admin.display_name}). "
            f"Visit /login to sign in."
        )
