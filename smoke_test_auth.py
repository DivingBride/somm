"""
End-to-end smoke test for Phase 0 auth + Phase 1 admin panel.

Phase 0 flow: invite accept -> magic-link request -> callback -> /api/me ->
logout. Phase 1 flow: admin-only gating, direct user creation, per-user
sign-in link generation (the Resend-free rail), user revocation with
revoke-on-next-request behaviour, invite CRUD, and audit-log writes.

Runs against an in-process httpx.ASGITransport against the real app and
a throwaway SQLite DB. Not a replacement for a real test suite — that
arrives in Phase 5 hardening — but proves the stack end to end.
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

        # ==================================================================
        # Phase 1 — admin panel flows
        # ==================================================================

        # 7. Non-admin cannot reach the admin API.
        r = await client.get("/api/admin/users", cookies={"somm_session": cookie})
        assert r.status_code == 403, (r.status_code, r.text)
        print("[smoke] regular user -> /api/admin/users -> 403")

        # 8-20. Admin flow uses a dedicated client so cookie jars don't
        # bleed between identities. `client` keeps newuser's cookie untouched
        # for the post-admin logout + revoke checks below.
        captured.clear()
        r = await client.post(
            "/api/auth/magic-link", json={"email": "founder@example.com"}
        )
        assert r.status_code == 200
        assert len(captured) == 1, "expected admin magic link"
        admin_token = captured[-1].split("token=", 1)[1]

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_cli:
            # 8. Admin login.
            r = await admin_cli.get(
                f"/auth/callback?token={admin_token}", follow_redirects=False
            )
            assert r.status_code == 303, (r.status_code, r.text)
            assert admin_cli.cookies.get("somm_session"), "expected admin session cookie"
            print("[smoke] admin logged in")

            # 9. Admin lists users.
            r = await admin_cli.get("/api/admin/users")
            assert r.status_code == 200, (r.status_code, r.text)
            emails = {u["email"] for u in r.json()["users"]}
            assert "founder@example.com" in emails
            assert "newuser@example.com" in emails
            print("[smoke] admin GET /api/admin/users OK:", sorted(emails))

            # 10. Admin creates a user directly (Resend-free delta 1).
            r = await admin_cli.post(
                "/api/admin/users",
                json={
                    "email": "charlie@example.com",
                    "display_name": "Charlie",
                    "role": "user",
                },
            )
            assert r.status_code == 200, (r.status_code, r.text)
            charlie = r.json()
            charlie_id = charlie["id"]
            assert charlie["email"] == "charlie@example.com"
            assert charlie["role"] == "user"
            print(f"[smoke] admin created user charlie id={charlie_id[:8]}")

            # 11. Admin issues a sign-in link for charlie (Resend-free delta 2).
            r = await admin_cli.post(
                f"/api/admin/users/{charlie_id}/magic-link"
            )
            assert r.status_code == 200, (r.status_code, r.text)
            payload = r.json()
            assert "url" in payload and "/auth/callback?token=" in payload["url"]
            charlie_token = payload["url"].split("token=", 1)[1]
            print("[smoke] admin issued magic link for charlie")

            # 12 + 13. Validate the issued link — charlie signs in, sees his
            # own /api/me, and is blocked from /api/admin/users.
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as charlie_cli:
                r = await charlie_cli.get(
                    f"/auth/callback?token={charlie_token}",
                    follow_redirects=False,
                )
                assert r.status_code == 303, (r.status_code, r.text)
                charlie_cookie = charlie_cli.cookies.get("somm_session")
                assert charlie_cookie, "expected charlie session cookie"

                r = await charlie_cli.get("/api/me")
                assert r.status_code == 200, (r.status_code, r.text)
                assert r.json()["email"] == "charlie@example.com"
                print("[smoke] charlie signed in via admin-issued link")

                r = await charlie_cli.get("/api/admin/users")
                assert r.status_code == 403, r.status_code
                print("[smoke] charlie -> /api/admin/users -> 403")

            # 14. Admin revokes charlie.
            r = await admin_cli.post(
                f"/api/admin/users/{charlie_id}/revoke"
            )
            assert r.status_code == 200, (r.status_code, r.text)
            assert r.json()["status"] == "revoked"
            print("[smoke] admin revoked charlie")

            # 15. Revoke-on-next-request: charlie's existing session -> 401.
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as replay:
                r = await replay.get(
                    "/api/me", cookies={"somm_session": charlie_cookie}
                )
                assert r.status_code == 401, (r.status_code, r.text)
            print("[smoke] revoked charlie's /api/me -> 401")

            # 16. Admin cannot revoke themselves.
            founder_id = next(
                u["id"]
                for u in (await admin_cli.get("/api/admin/users")).json()["users"]
                if u["email"] == "founder@example.com"
            )
            r = await admin_cli.post(f"/api/admin/users/{founder_id}/revoke")
            assert r.status_code == 400, (r.status_code, r.text)
            print("[smoke] admin self-revoke blocked")

            # 17. Create + delete an unused invite; second delete -> 404.
            r = await admin_cli.post(
                "/api/admin/invites", json={"expires_in_days": 3}
            )
            assert r.status_code == 200, (r.status_code, r.text)
            invite1 = r.json()
            assert "share_url" in invite1 and "/invite/" in invite1["share_url"]
            print(f"[smoke] admin created invite id={invite1['id'][:8]}")

            r = await admin_cli.get("/api/admin/invites")
            assert r.status_code == 200
            assert any(i["id"] == invite1["id"] for i in r.json()["invites"])

            r = await admin_cli.delete(f"/api/admin/invites/{invite1['id']}")
            assert r.status_code == 200, (r.status_code, r.text)

            r = await admin_cli.delete(f"/api/admin/invites/{invite1['id']}")
            assert r.status_code == 404, r.status_code
            print("[smoke] unused invite revoke OK; second revoke -> 404")

            # 18. Create + accept invite, then try to revoke -> 400.
            r = await admin_cli.post(
                "/api/admin/invites",
                json={"email": "dave@example.com", "expires_in_days": 3},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            invite2 = r.json()

            captured.clear()
            r = await admin_cli.post(
                "/api/auth/accept-invite",
                json={
                    "invite_token": invite2["token"],
                    "email": "dave@example.com",
                    "display_name": "Dave",
                },
            )
            assert r.status_code == 200, (r.status_code, r.text)
            assert len(captured) == 1, "accept-invite should email the new user"

            r = await admin_cli.delete(f"/api/admin/invites/{invite2['id']}")
            assert r.status_code == 400, (r.status_code, r.text)
            print("[smoke] used invite cannot be revoked -> 400")

            # 19. Audit log contains the mutating admin actions.
            r = await admin_cli.get("/api/admin/audit?limit=500")
            assert r.status_code == 200, (r.status_code, r.text)
            actions = {e["action"] for e in r.json()["events"]}
            for expected in (
                "user_created",
                "user_revoked",
                "admin_magic_link_issued",
                "invite_created",
                "invite_revoked",
            ):
                assert expected in actions, (expected, actions)
            print("[smoke] audit log has expected actions:", sorted(actions))

            # 20. Audit filter by action narrows results.
            r = await admin_cli.get("/api/admin/audit?action=user_created")
            assert r.status_code == 200
            assert all(e["action"] == "user_created" for e in r.json()["events"])
            print("[smoke] audit filter by action OK")

        # ==================================================================
        # Phase 0 flow completion — logout + one-time use (uses the original
        # newuser `cookie` which is still valid at this point).
        # ==================================================================

        # 21. Logout clears the session.
        r = await client.post(
            "/api/auth/logout",
            cookies={"somm_session": cookie},
        )
        assert r.status_code == 200
        print("[smoke] logout OK")

        # 22. /api/me with the now-revoked cookie → 401. Use a fresh client
        # so only the explicitly-passed cookie is in play.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as replay:
            r = await replay.get("/api/me", cookies={"somm_session": cookie})
            assert r.status_code == 401, r.status_code
        print("[smoke] post-logout /api/me -> 401 (session revoked)")

        # 23. Magic link is one-time-use: replaying the invite token should 400.
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
