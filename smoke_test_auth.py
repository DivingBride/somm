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

# Build a tiny Chartier xlsx fixture so the startup sync has something
# real to ingest. Two header rows + four data rows that collectively
# cover the q / wine_style / molecule_family filter paths.
from openpyxl import Workbook  # noqa: E402

_xlsx_path = Path(_tmpdir) / "fixture_chartier.xlsx"
_wb = Workbook()
_ws = _wb.active
_ws.append(["Wine Style", "Dish", "Molecule Logic", "Wine Examples"])  # row 1 header
_ws.append(["--", "--", "--", "--"])                                    # row 2 header
_ws.append([
    "Gewürztraminer (aromatic white)",
    "Thai green curry with basil",
    "Shared linalool & rose-family terpenes in wine and ginger/lemongrass",
    "Trimbach Gewurz 2019; Zind-Humbrecht Clos Windsbuhl",
])
_ws.append([
    "Syrah (peppery red)",
    "Grilled lamb with black pepper crust",
    "Rotundone in Syrah mirrors the pepper molecules on the lamb",
    "Côte-Rôtie E. Guigal; Crozes-Hermitage Graillot",
])
_ws.append([
    "Sauvignon Blanc",
    "Asparagus with goat cheese",
    "Pyrazines in Sauvignon echo the green notes in asparagus",
    "Sancerre Vacheron; Marlborough Cloudy Bay",
])
_ws.append([
    "Chardonnay (oaked)",
    "Roast chicken with tarragon",
    "Whisky-lactone and vanillin from oak pair with herbal tarragon notes",
    "Meursault Coche-Dury; Sonoma Kistler",
])
_wb.save(_xlsx_path)
os.environ["CHARTIER_XLSX_PATH"] = str(_xlsx_path)

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

    # raise_app_exceptions=False lets the app's registered 5xx handler
    # (observability.unhandled_exception_handler) convert uncaught
    # exceptions into 500 responses — without this flag httpx re-raises
    # them to the test and the handler never runs.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
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
        newuser_id = me["id"]
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

            # ================================================================
            # Phase 2 — chat persistence
            #
            # Stub out main.client.messages.create so we never hit the real
            # Anthropic endpoint from CI. The stub returns a trivial text
            # block; the chat router only reads `response.content[0].text`.
            # ================================================================
            import main as _main

            class _StubContent:
                def __init__(self, text): self.text = text

            class _StubResponse:
                def __init__(self, text): self.content = [_StubContent(text)]

            _stub_calls = {"count": 0, "systems": []}

            def _stub_create(**kwargs):
                _stub_calls["count"] += 1
                _stub_calls["systems"].append(kwargs.get("system") or "")
                # Title-gen asks for a 3-7 word title; the main pairing call
                # uses the long Chartier prompt. Return a short string so the
                # title extractor in chats.py is happy either way.
                return _StubResponse("Stubbed sommelier reply.")

            def _find_pairing_system():
                """Pick the main pairing call (the one with REFERENCE TABLES)."""
                for s in _stub_calls["systems"]:
                    if "REFERENCE TABLES" in s:
                        return s
                return ""

            _orig_create = _main.client.messages.create
            _main.client.messages.create = _stub_create
            try:
                # 21. newuser: list chats → empty.
                r = await client.get("/api/chats")
                assert r.status_code == 200, (r.status_code, r.text)
                assert r.json()["chats"] == []
                print("[smoke] newuser /api/chats empty initially")

                # 22. newuser: create an empty chat.
                r = await client.post("/api/chats", json={})
                assert r.status_code == 200, (r.status_code, r.text)
                chat = r.json()
                chat_id = chat["id"]
                assert chat["user_id"] == newuser_id
                assert chat["title"] is None
                print(f"[smoke] newuser created chat id={chat_id[:8]}")

                # 23. newuser: post a message → user+assistant persisted.
                r = await client.post(
                    f"/api/chats/{chat_id}/messages",
                    json={"content": "Pair a wine with rosemary chicken please"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                payload = r.json()
                assert payload["user_message"]["role"] == "user"
                assert payload["assistant_message"]["role"] == "assistant"
                assert payload["assistant_message"]["content"] == "Stubbed sommelier reply."
                # attachments_json captures routed screenshot prefixes (rosemary → p124).
                attachments = payload["user_message"]["attachments_json"]
                assert attachments and "p124" in attachments, attachments
                assert _stub_calls["count"] >= 1
                print("[smoke] POST message persisted + screenshot prefix captured:", attachments)

                # 24. newuser: GET the chat → shows both messages in order.
                r = await client.get(f"/api/chats/{chat_id}")
                assert r.status_code == 200
                body = r.json()
                roles = [m["role"] for m in body["messages"]]
                assert roles == ["user", "assistant"], roles
                print("[smoke] GET chat returns full message history")

                # 25. newuser: rename the chat.
                r = await client.patch(
                    f"/api/chats/{chat_id}", json={"title": "Rosemary dinner"}
                )
                assert r.status_code == 200, (r.status_code, r.text)
                assert r.json()["title"] == "Rosemary dinner"

                # Empty title is rejected.
                r = await client.patch(
                    f"/api/chats/{chat_id}", json={"title": "   "}
                )
                assert r.status_code == 400
                print("[smoke] rename OK; empty title -> 400")

                # 26. charlie-style cross-user isolation: create a fresh user,
                # log them in, confirm they cannot read newuser's chat.
                r = await admin_cli.post(
                    "/api/admin/users",
                    json={"email": "eve@example.com", "role": "user"},
                )
                assert r.status_code == 200
                eve_id = r.json()["id"]
                r = await admin_cli.post(f"/api/admin/users/{eve_id}/magic-link")
                eve_token = r.json()["url"].split("token=", 1)[1]

                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as eve_cli:
                    await eve_cli.get(
                        f"/auth/callback?token={eve_token}", follow_redirects=False
                    )
                    r = await eve_cli.get(f"/api/chats/{chat_id}")
                    assert r.status_code == 403, (r.status_code, r.text)

                    # Eve also can't rename or archive newuser's chat.
                    r = await eve_cli.patch(
                        f"/api/chats/{chat_id}", json={"title": "hijack"}
                    )
                    assert r.status_code == 403, r.status_code
                    r = await eve_cli.delete(f"/api/chats/{chat_id}")
                    assert r.status_code == 403, r.status_code
                    print("[smoke] cross-user chat access -> 403")

                # 27. Admin sees newuser's chat via /api/admin/users/{id}/chats.
                r = await admin_cli.get(f"/api/admin/users/{newuser_id}/chats")
                assert r.status_code == 200, (r.status_code, r.text)
                admin_view = r.json()
                assert admin_view["user"]["id"] == newuser_id
                assert any(c["id"] == chat_id for c in admin_view["chats"])
                print("[smoke] admin GET /api/admin/users/{id}/chats OK")

                # 28. newuser: archive the chat → no longer in list, but still
                # visible to admin (archived_at set).
                r = await client.delete(f"/api/chats/{chat_id}")
                assert r.status_code == 200, (r.status_code, r.text)

                r = await client.get("/api/chats")
                assert all(c["id"] != chat_id for c in r.json()["chats"])

                r = await admin_cli.get(f"/api/admin/users/{newuser_id}/chats")
                archived = next(
                    c for c in r.json()["chats"] if c["id"] == chat_id
                )
                assert archived["archived_at"] is not None
                print("[smoke] archive hides from user list but admin still sees it")

                # 29. Posting to an archived chat -> 400.
                r = await client.post(
                    f"/api/chats/{chat_id}/messages",
                    json={"content": "anything"},
                )
                assert r.status_code == 400, (r.status_code, r.text)
                print("[smoke] post to archived chat -> 400")

                # 30. Empty-content post -> 400.
                r = await client.post("/api/chats", json={})
                fresh_chat_id = r.json()["id"]
                r = await client.post(
                    f"/api/chats/{fresh_chat_id}/messages",
                    json={"content": "   "},
                )
                assert r.status_code == 400, r.status_code
                print("[smoke] empty message -> 400")

                # ============================================================
                # Phase 3 — preferences (§7, §10)
                # ============================================================

                # 31. newuser: GET /api/preferences -> empty.
                r = await client.get("/api/preferences")
                assert r.status_code == 200, (r.status_code, r.text)
                assert r.json()["preferences"] == []
                print("[smoke] /api/preferences empty initially")

                # 32. Invalid kind/strictness/value are rejected.
                r = await client.post(
                    "/api/preferences",
                    json={"kind": "not-a-kind", "value": "x", "strictness": "strict"},
                )
                assert r.status_code == 400, r.status_code
                r = await client.post(
                    "/api/preferences",
                    json={"kind": "grape", "value": "x", "strictness": "nope"},
                )
                assert r.status_code == 400, r.status_code
                r = await client.post(
                    "/api/preferences",
                    json={"kind": "grape", "value": "   ", "strictness": "strict"},
                )
                assert r.status_code == 400, r.status_code
                print("[smoke] preference validation rejects bad kind/strictness/value")

                # 33. Create a STRICT Gewürztraminer pref — the spec's
                # worked example (§7).
                r = await client.post(
                    "/api/preferences",
                    json={
                        "kind": "grape",
                        "value": "Gewürztraminer",
                        "strictness": "strict",
                        "notes": "floral wines give me a headache",
                    },
                )
                assert r.status_code == 200, (r.status_code, r.text)
                strict_pref = r.json()
                assert strict_pref["strictness"] == "strict"
                assert strict_pref["kind"] == "grape"
                assert strict_pref["value"] == "Gewürztraminer"
                assert strict_pref["notes"] == "floral wines give me a headache"
                print(f"[smoke] created strict pref id={strict_pref['id'][:8]}")

                # 34. Create a SOFT Chardonnay pref.
                r = await client.post(
                    "/api/preferences",
                    json={
                        "kind": "grape",
                        "value": "Chardonnay",
                        "strictness": "soft",
                    },
                )
                assert r.status_code == 200, (r.status_code, r.text)
                soft_pref = r.json()
                assert soft_pref["strictness"] == "soft"
                print(f"[smoke] created soft pref id={soft_pref['id'][:8]}")

                # 35. GET now lists both.
                r = await client.get("/api/preferences")
                assert r.status_code == 200
                prefs_listing = r.json()["preferences"]
                assert len(prefs_listing) == 2
                kinds = {p["value"] for p in prefs_listing}
                assert kinds == {"Gewürztraminer", "Chardonnay"}
                print("[smoke] prefs listing returns both rows")

                # 36. PATCH strictness: soft → strict, then back.
                r = await client.patch(
                    f"/api/preferences/{soft_pref['id']}",
                    json={"strictness": "strict"},
                )
                assert r.status_code == 200
                assert r.json()["strictness"] == "strict"
                r = await client.patch(
                    f"/api/preferences/{soft_pref['id']}",
                    json={"strictness": "soft", "notes": "usually too oaky"},
                )
                assert r.status_code == 200
                assert r.json()["strictness"] == "soft"
                assert r.json()["notes"] == "usually too oaky"
                print("[smoke] PATCH strictness + notes OK")

                # 37. WORKED EXAMPLE (§7). Post the Thai-green-curry question;
                # verify the system prompt injected by chats.py contains the
                # HARD CONSTRAINTS section with Gewürztraminer listed.
                r = await client.post("/api/chats", json={})
                worked_chat_id = r.json()["id"]
                _stub_calls["systems"].clear()
                r = await client.post(
                    f"/api/chats/{worked_chat_id}/messages",
                    json={
                        "content": "I'm making Thai green curry — what wine?",
                    },
                )
                assert r.status_code == 200, (r.status_code, r.text)
                # Let the fire-and-forget title task run too, so all calls
                # are captured — we filter to the pairing call below anyway.
                await asyncio.sleep(0.05)
                sys_prompt = _find_pairing_system()
                assert "USER HARD CONSTRAINTS" in sys_prompt, "hard-constraints section missing"
                assert "USER SOFT PREFERENCES" in sys_prompt, "soft-preferences section missing"
                assert "RANKING RULE" in sys_prompt, "ranking rule missing"
                assert "Gewürztraminer" in sys_prompt, "strict pref not present in prompt"
                assert "Chardonnay" in sys_prompt, "soft pref not present in prompt"
                # The base Chartier prompt is still present (§7 injects on top, does not replace).
                assert "REFERENCE TABLES" in sys_prompt, "base SYSTEM_PROMPT was replaced not augmented"
                print("[smoke] Thai-green-curry worked example: injected block present + base prompt intact")

                # 38. Cross-user isolation: eve cannot see newuser's prefs,
                # and cannot PATCH / DELETE them.
                r = await admin_cli.post(
                    "/api/admin/users",
                    json={"email": "mallory@example.com", "role": "user"},
                )
                mallory_id = r.json()["id"]
                r = await admin_cli.post(
                    f"/api/admin/users/{mallory_id}/magic-link"
                )
                mallory_token = r.json()["url"].split("token=", 1)[1]
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as mallory_cli:
                    await mallory_cli.get(
                        f"/auth/callback?token={mallory_token}",
                        follow_redirects=False,
                    )
                    # Mallory's own list is empty — she sees no rows at all.
                    r = await mallory_cli.get("/api/preferences")
                    assert r.status_code == 200
                    assert r.json()["preferences"] == []

                    # PATCH / DELETE on newuser's pref -> 403.
                    r = await mallory_cli.patch(
                        f"/api/preferences/{strict_pref['id']}",
                        json={"strictness": "soft"},
                    )
                    assert r.status_code == 403, r.status_code
                    r = await mallory_cli.delete(
                        f"/api/preferences/{strict_pref['id']}"
                    )
                    assert r.status_code == 403, r.status_code
                    print("[smoke] cross-user pref access -> 403")

                # Confirm the strict pref is still intact and unchanged.
                r = await client.get("/api/preferences")
                assert r.status_code == 200
                still_strict = next(
                    p for p in r.json()["preferences"]
                    if p["id"] == strict_pref["id"]
                )
                assert still_strict["strictness"] == "strict"

                # 39. DELETE both prefs; GET returns empty again.
                for pid in (strict_pref["id"], soft_pref["id"]):
                    r = await client.delete(f"/api/preferences/{pid}")
                    assert r.status_code == 200, (r.status_code, r.text)
                r = await client.get("/api/preferences")
                assert r.json()["preferences"] == []
                # Double-delete -> 404.
                r = await client.delete(f"/api/preferences/{strict_pref['id']}")
                assert r.status_code == 404
                print("[smoke] delete prefs -> empty; double-delete -> 404")

                # 40. Zero-preference invariant (§11 step 4): chats.py must
                # pass the base SYSTEM_PROMPT unmodified when the user has
                # no prefs. Prove it by posting a message and byte-comparing
                # the pairing-call system prompt against main.SYSTEM_PROMPT.
                import main as _main_again
                _stub_calls["systems"].clear()
                r = await client.post("/api/chats", json={})
                zero_chat_id = r.json()["id"]
                r = await client.post(
                    f"/api/chats/{zero_chat_id}/messages",
                    json={"content": "Pair a wine with steak."},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                await asyncio.sleep(0.05)
                pairing_sys = _find_pairing_system()
                assert pairing_sys == _main_again.SYSTEM_PROMPT, (
                    "zero-pref user must see base SYSTEM_PROMPT unchanged"
                )
                print("[smoke] zero-preference user sees identical v1 behaviour")
            finally:
                _main.client.messages.create = _orig_create

            # ================================================================
            # Phase 4 — Chartier library + saved pairings (§8, §9)
            # ================================================================

            # 41. Admin-triggered sync: ingests all 4 fixture rows.
            r = await admin_cli.post("/api/admin/chartier/sync")
            assert r.status_code == 200, (r.status_code, r.text)
            summary1 = r.json()
            assert summary1["total"] == 4, summary1
            assert summary1["added"] == 4, summary1
            assert summary1["unchanged"] == 0, summary1
            print("[smoke] admin chartier sync ingested 4 rows:", summary1)

            # 42. Second sync is idempotent: all unchanged.
            r = await admin_cli.post("/api/admin/chartier/sync")
            assert r.status_code == 200
            summary2 = r.json()
            assert summary2["total"] == 4, summary2
            assert summary2["added"] == 0, summary2
            assert summary2["unchanged"] == 4, summary2
            print("[smoke] second chartier sync idempotent:", summary2)

            # 43. Audit log records chartier_synced.
            r = await admin_cli.get("/api/admin/audit?action=chartier_synced")
            assert r.status_code == 200
            assert len(r.json()["events"]) >= 2, "expected 2 chartier_synced events"
            print("[smoke] audit log captured chartier_synced events")

            # 44. Non-admin cannot trigger sync.
            r = await client.post("/api/admin/chartier/sync")
            assert r.status_code == 403, r.status_code
            print("[smoke] non-admin chartier sync -> 403")

            # 45. Any signed-in user can read the library + facets.
            r = await client.get("/api/chartier")
            assert r.status_code == 200, (r.status_code, r.text)
            body = r.json()
            assert body["total"] == 4, body
            assert len(body["entries"]) == 4
            fam_names = set(body["facets"]["molecule_families"])
            # Exact bucket names from chartier.MOLECULE_FAMILIES.
            assert "rotundone / pepper" in fam_names, fam_names
            assert "pyrazines / green" in fam_names, fam_names
            assert "whisky-lactone / oak" in fam_names, fam_names
            style_names = set(body["facets"]["wine_styles"])
            # wine_style facet collapses parentheticals — see chartier.py parsing.
            assert "Gewürztraminer" in style_names, style_names
            assert "Syrah" in style_names, style_names
            print("[smoke] /api/chartier lists all rows + facets:", sorted(fam_names))

            # 46. Text search narrows results.
            r = await client.get("/api/chartier", params={"q": "curry"})
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == 1, body
            assert "curry" in body["entries"][0]["dish"].lower()
            thai_id = body["entries"][0]["id"]
            print(f"[smoke] q=curry narrowed to 1 row id={thai_id}")

            # 47. wine_style filter narrows results.
            r = await client.get("/api/chartier", params={"wine_style": "Syrah"})
            assert r.status_code == 200
            syrah_rows = r.json()["entries"]
            assert len(syrah_rows) == 1
            assert "Syrah" in syrah_rows[0]["wine_style"]
            print("[smoke] wine_style=Syrah narrowed to 1 row")

            # 48. molecule_family filter narrows results.
            r = await client.get(
                "/api/chartier", params={"molecule_family": "pyrazines / green"}
            )
            assert r.status_code == 200
            green_rows = r.json()["entries"]
            assert len(green_rows) == 1
            assert "asparagus" in green_rows[0]["dish"].lower()
            print("[smoke] molecule_family filter narrowed to 1 row")

            # 49. GET /api/chartier/{id} for single-entry fetch + 404 path.
            r = await client.get(f"/api/chartier/{thai_id}")
            assert r.status_code == 200
            assert r.json()["id"] == thai_id
            r = await client.get("/api/chartier/99999")
            assert r.status_code == 404
            print("[smoke] single-entry fetch OK; missing id -> 404")

            # 50. Saved pairings: empty listing initially.
            r = await client.get("/api/saved")
            assert r.status_code == 200
            assert r.json()["saved"] == []
            print("[smoke] /api/saved empty initially")

            # 51. Save from chartier — server snapshots fields.
            r = await client.post(
                "/api/saved",
                json={"source": "chartier", "source_ref": str(thai_id)},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            saved_from_chartier = r.json()
            assert saved_from_chartier["source"] == "chartier"
            assert "Gewürztraminer" in (saved_from_chartier["wine"] or "")
            assert "curry" in (saved_from_chartier["dish"] or "").lower()
            assert saved_from_chartier["tried_at"] is None
            assert saved_from_chartier["is_private"] is True
            print(f"[smoke] chartier-sourced save id={saved_from_chartier['id'][:8]}")

            # 52. Save from chat — free-form fields.
            r = await client.post(
                "/api/saved",
                json={
                    "source": "chat",
                    "source_ref": "msg-xyz",
                    "wine": "Pinot Noir, Central Otago",
                    "dish": "Duck breast with cherry sauce",
                    "user_notes": "Tried at Rick's dinner party",
                },
            )
            assert r.status_code == 200, (r.status_code, r.text)
            saved_from_chat = r.json()
            assert saved_from_chat["source"] == "chat"
            assert saved_from_chat["source_ref"] == "msg-xyz"
            print("[smoke] chat-sourced save OK")

            # 53. Save custom — wine + dish required.
            r = await client.post(
                "/api/saved",
                json={"source": "custom", "wine": "", "dish": ""},
            )
            assert r.status_code == 400, r.status_code
            r = await client.post(
                "/api/saved",
                json={
                    "source": "custom",
                    "wine": "Riesling Spätlese",
                    "dish": "Pork belly with apple",
                    "molecule_logic": "Off-dry sweetness balances caramelized pork fat.",
                },
            )
            assert r.status_code == 200, (r.status_code, r.text)
            saved_custom = r.json()
            assert saved_custom["source"] == "custom"
            print("[smoke] custom save OK; empty wine+dish -> 400")

            # 54. GET lists all three.
            r = await client.get("/api/saved")
            assert r.status_code == 200
            listing = r.json()["saved"]
            assert len(listing) == 3, listing
            sources = {p["source"] for p in listing}
            assert sources == {"chartier", "chat", "custom"}, sources
            print("[smoke] /api/saved lists all three sources")

            # 55. PATCH notes + tried_at.
            r = await client.patch(
                f"/api/saved/{saved_from_chartier['id']}",
                json={"user_notes": "Loved it — making again Sunday"},
            )
            assert r.status_code == 200
            assert "Sunday" in r.json()["user_notes"]

            tried_ts = _now()
            r = await client.patch(
                f"/api/saved/{saved_from_chartier['id']}",
                json={"tried_at": tried_ts},
            )
            assert r.status_code == 200
            assert r.json()["tried_at"] == tried_ts

            # Empty tried_at clears.
            r = await client.patch(
                f"/api/saved/{saved_from_chartier['id']}",
                json={"tried_at": ""},
            )
            assert r.status_code == 200
            assert r.json()["tried_at"] is None
            print("[smoke] PATCH notes + tried_at set/clear OK")

            # 56. Cross-user isolation: fresh user cannot read/patch/delete.
            r = await admin_cli.post(
                "/api/admin/users",
                json={"email": "nick@example.com", "role": "user"},
            )
            nick_id = r.json()["id"]
            r = await admin_cli.post(f"/api/admin/users/{nick_id}/magic-link")
            nick_token = r.json()["url"].split("token=", 1)[1]
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as nick_cli:
                await nick_cli.get(
                    f"/auth/callback?token={nick_token}", follow_redirects=False
                )
                # Nick's own list is empty.
                r = await nick_cli.get("/api/saved")
                assert r.status_code == 200
                assert r.json()["saved"] == []

                # PATCH / DELETE on newuser's rows -> 403.
                r = await nick_cli.patch(
                    f"/api/saved/{saved_from_chartier['id']}",
                    json={"user_notes": "hijack"},
                )
                assert r.status_code == 403, r.status_code
                r = await nick_cli.delete(
                    f"/api/saved/{saved_from_chartier['id']}"
                )
                assert r.status_code == 403, r.status_code
                print("[smoke] cross-user saved access -> 403")

            # 57. DELETE; double-delete -> 404.
            for pid in (
                saved_from_chartier["id"],
                saved_from_chat["id"],
                saved_custom["id"],
            ):
                r = await client.delete(f"/api/saved/{pid}")
                assert r.status_code == 200, (r.status_code, r.text)
            r = await client.get("/api/saved")
            assert r.json()["saved"] == []
            r = await client.delete(f"/api/saved/{saved_from_chartier['id']}")
            assert r.status_code == 404
            print("[smoke] delete saved rows -> empty; double-delete -> 404")

            # 58. Chartier save with missing source_ref / bad source.
            r = await client.post("/api/saved", json={"source": "chartier"})
            assert r.status_code == 400, r.status_code
            r = await client.post(
                "/api/saved", json={"source": "chartier", "source_ref": "99999"}
            )
            assert r.status_code == 404, r.status_code
            r = await client.post("/api/saved", json={"source": "bogus"})
            assert r.status_code == 400, r.status_code
            print("[smoke] chartier-save validation + bad source rejected")

        # ==================================================================
        # Phase 0 flow completion — logout + one-time use (uses the original
        # newuser `cookie` which is still valid at this point).
        # ==================================================================

        # 59. Logout clears the session.
        r = await client.post(
            "/api/auth/logout",
            cookies={"somm_session": cookie},
        )
        assert r.status_code == 200
        print("[smoke] logout OK")

        # 60. /api/me with the now-revoked cookie → 401. Use a fresh client
        # so only the explicitly-passed cookie is in play.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as replay:
            r = await replay.get("/api/me", cookies={"somm_session": cookie})
            assert r.status_code == 401, r.status_code
        print("[smoke] post-logout /api/me -> 401 (session revoked)")

        # 61. Magic link is one-time-use: replaying the invite token should 400.
        r = await client.get(
            f"/auth/callback?token={token}", follow_redirects=False
        )
        assert r.status_code == 400, r.status_code
        print("[smoke] magic link is one-time-use")

        # ==================================================================
        # Phase 5 — hardening (§12, §13.3)
        # ==================================================================

        # 62. /healthz returns a 200 snapshot with per-subsystem status.
        r = await client.get("/healthz")
        assert r.status_code == 200, (r.status_code, r.text)
        snap = r.json()
        assert "status" in snap and "checks" in snap, snap
        # In the smoke environment: DB is ok, xlsx exists (we wrote a
        # fixture), email is 'disabled' (no RESEND_API_KEY) so overall
        # status is 'degraded' — still 200, not 503.
        assert snap["checks"]["db"]["status"] == "ok", snap
        assert snap["checks"]["chartier_xlsx"]["status"] == "ok", snap
        assert snap["checks"]["email"]["status"] in ("ok", "disabled"), snap
        print("[smoke] /healthz snapshot:", snap["status"], list(snap["checks"].keys()))

        # 63. Rate limit on /api/auth/magic-link: 3 per 5 min per email.
        # Reset the limiter first so this test is independent of earlier
        # magic-link calls in this run (step 5 already consumed one slot
        # for newuser@example.com).
        import observability
        observability.magic_link_rate_limiter.reset()

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as rl_cli:
            ok_count = 0
            limited_count = 0
            for _ in range(5):
                rr = await rl_cli.post(
                    "/api/auth/magic-link",
                    json={"email": "floodtest@example.com"},
                )
                if rr.status_code == 200:
                    ok_count += 1
                elif rr.status_code == 429:
                    limited_count += 1
                    assert "Retry-After" in rr.headers, rr.headers
                else:
                    raise AssertionError((rr.status_code, rr.text))
            assert ok_count == 3, ok_count
            assert limited_count == 2, limited_count
        print(f"[smoke] magic-link rate limit: {ok_count} allowed, {limited_count} 429s")

        # 64. Rate limit is per-email: a different email still gets through
        # on the next call.
        rr = await client.post(
            "/api/auth/magic-link",
            json={"email": "otheremail@example.com"},
        )
        assert rr.status_code == 200, (rr.status_code, rr.text)
        print("[smoke] rate limiter is per-email (other address not blocked)")

        # 65. 5xx exception handler: mount a synthetic route that raises,
        # verify the generic 500 body + that send_error_email was invoked
        # (captured via monkeypatch — we don't actually send mail).
        sent_alerts: list[tuple[str, str]] = []
        orig_sender = observability.send_error_email

        def _capture(subject, body):
            sent_alerts.append((subject, body))

        observability.send_error_email = _capture
        try:
            @app.get("/__smoke_boom")
            async def _boom():
                raise RuntimeError("simulated failure for smoke test")

            rr = await client.get("/__smoke_boom")
            assert rr.status_code == 500, (rr.status_code, rr.text)
            body = rr.json()
            # Response body is generic — never leaks the exception type or
            # traceback to the client.
            assert "simulated failure" not in rr.text, rr.text
            assert "detail" in body and "Internal server error" in body["detail"]
            # But the admin alert captured the full details.
            assert len(sent_alerts) == 1, sent_alerts
            subj, txt = sent_alerts[0]
            assert "/__smoke_boom" in subj
            assert "simulated failure" in txt
            assert "Traceback" in txt
            print("[smoke] 5xx handler: generic body + admin alert captured")
        finally:
            observability.send_error_email = orig_sender

        # 66. /healthz reports 503 when DB is broken. Monkeypatch the
        # session factory to raise so the db check fails.
        import observability as _obs
        orig_snap = _obs.healthz_snapshot

        async def _broken_snapshot():
            snap = await orig_snap()
            snap["checks"]["db"] = {"status": "fail", "detail": "simulated"}
            snap["status"] = "fail"
            return snap

        _obs.healthz_snapshot = _broken_snapshot
        try:
            rr = await client.get("/healthz")
            assert rr.status_code == 503, (rr.status_code, rr.text)
            assert rr.json()["status"] == "fail"
            print("[smoke] /healthz returns 503 when subsystem fails")
        finally:
            _obs.healthz_snapshot = orig_snap

    print("[smoke] ALL CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        # Clean up the throwaway DB.
        import shutil

        shutil.rmtree(_tmpdir, ignore_errors=True)
