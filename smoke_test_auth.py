"""
End-to-end smoke test for Phase 0 auth.

Exercises: invite accept -> magic-link request -> callback -> /api/me ->
logout. Runs against an in-process httpx.ASGITransport against the real app
and a throwaway SQLite DB. Not a replacement for a real test suite — that
arrives in Phase 5 hardening — but proves the Phase 0 pipe end to end.
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

# Point at a throwaway DB before importing anything that touches `db`.
_tmpdir = tempfile.mkdtemp(prefix="somm_smoke_")
_dbfile = Path(_tmpdir) / "smoke.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_dbfile}"
os.environ["SESSION_SECRET"] = "smoke-test-secret-not-for-production-use"
os.environ["COOKIE_SECURE"] = "false"
os.environ["APP_URL"] = "http://testserver"
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-placeholder")
os.environ.setdefault("APP_PASSWORD", "dev")

# Run migrations against the throwaway DB.
from alembic import command
from alembic.config import Config

cfg = Config(str(Path(__file__).parent / "alembic.ini"))
cfg.set_main_option("sqlalchemy.url", f"sqlite:///{_dbfile}")
command.upgrade(cfg, "head")

import httpx  # noqa: E402

from main import app  # noqa: E402
from db import async_session_factory  # noqa: E402
from models import Invite, MagicLinkToken, User  # noqa: E402
from sqlalchemy import select  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _seed_admin_and_invite():
    async with async_session_factory() as db:
        admin = User(
            id=str(uuid.uuid4()),
            email="founder@example.com",
            display_name="Founder",
            role="admin",
            status="active",
            created_at=_now(),
        )
        db.add(admin)
        invite = Invite(
            id=str(uuid.uuid4()),
            token="test-invite-token-abc123",
            email=None,
            created_by=admin.id,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )
        db.add(invite)
        await db.commit()
        return admin.id, invite.token


async def _latest_magic_link_raw_for(email: str) -> str:
    """
    Dev-only helper: we can't intercept the email, but since RESEND_API_KEY
    is unset the app prints the URL to stdout. For tests we pull the token
    straight from the DB — but only the hash is there. So instead we patch
    auth._send_magic_link_email in-place to capture the link.
    """
    raise NotImplementedError


async def run():
    admin_id, invite_token = await _seed_admin_and_invite()
    print(f"[smoke] seeded admin={admin_id[:8]} invite={invite_token}")

    # Capture magic-link URLs by monkeypatching the email sender.
    import auth

    captured: list[str] = []
    orig = auth._send_magic_link_email

    def _capture(to_email, link):
        captured.append(link)
        orig(to_email, link)

    auth._send_magic_link_email = _capture

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # 1. Accept invite → creates user, issues magic link.
        r = await client.post(
            "/api/auth/accept-invite",
            json={
                "invite_token": invite_token,
                "email": "newuser@example.com",
                "display_name": "New User",
            },
        )
        assert r.status_code == 200, (r.status_code, r.text)
        print("[smoke] accept-invite OK:", r.json())
        assert len(captured) == 1, captured
        invite_link = captured[-1]

        # 2. Hit the callback → sets session cookie.
        token = invite_link.split("token=", 1)[1]
        r = await client.get(f"/auth/callback?token={token}", follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text)
        cookie = r.cookies.get("somm_session")
        assert cookie, "expected somm_session cookie"
        print("[smoke] callback set session cookie (len=%d)" % len(cookie))

        # 3. /api/me with cookie → should succeed.
        r = await client.get("/api/me", cookies={"somm_session": cookie})
        assert r.status_code == 200, (r.status_code, r.text)
        me = r.json()
        assert me["email"] == "newuser@example.com"
        assert me["role"] == "user"
        print("[smoke] /api/me OK:", me)

        # 4. /api/me with no cookie → 401. (httpx persists a cookie jar,
        # so we use a fresh client to simulate an unauthenticated caller.)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as anon:
            r = await anon.get("/api/me")
            assert r.status_code == 401, r.status_code
        print("[smoke] /api/me unauth -> 401 (as expected)")

        # 5. Second-round magic link for existing user.
        captured.clear()
        r = await client.post(
            "/api/auth/magic-link",
            json={"email": "newuser@example.com"},
        )
        assert r.status_code == 200
        assert len(captured) == 1, "expected 1 email for active user"
        print("[smoke] magic-link resend OK")

        # 6. Generic response for unknown email — no enumeration.
        captured.clear()
        r = await client.post(
            "/api/auth/magic-link",
            json={"email": "nobody@example.com"},
        )
        assert r.status_code == 200
        assert len(captured) == 0, "must not email unknown addresses"
        print("[smoke] unknown-email path stays silent (no enumeration)")

        # 7. Logout clears the session.
        r = await client.post(
            "/api/auth/logout",
            cookies={"somm_session": cookie},
        )
        assert r.status_code == 200
        print("[smoke] logout OK")

        # 8. /api/me with the now-revoked cookie → 401. Use a fresh client
        # so only the explicitly-passed cookie is in play.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as replay:
            r = await replay.get("/api/me", cookies={"somm_session": cookie})
            assert r.status_code == 401, r.status_code
        print("[smoke] post-logout /api/me -> 401 (session revoked)")

        # 9. Magic link is one-time-use: replaying the invite token should 400.
        r = await client.get(
            f"/auth/callback?token={token}", follow_redirects=False
        )
        assert r.status_code == 400, r.status_code
        print("[smoke] magic link is one-time-use")

    print("[smoke] ALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        # Clean up the throwaway DB.
        import shutil

        shutil.rmtree(_tmpdir, ignore_errors=True)
