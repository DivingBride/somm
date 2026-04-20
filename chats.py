"""
Chat persistence API — Phase 2 of the v2.0 spec (§9, §10).

All endpoints are gated by `auth.get_current_user`. A user may only read/
mutate their own chats; admins may additionally read any user's chats via
the admin.py surface (§10).

On first user message in a chat, we fire-and-forget an async task that
asks Claude for a short title (one line, no quotes) and writes it back
to chats.title.

Per-turn context is the last MAX_HISTORY_MESSAGES messages in that chat,
mirroring the v1 in-memory behaviour. Screenshot routing stays intact
(§9) and the chosen prefixes are persisted to messages.attachments_json
so an admin reviewing a chat can see which images were attached to each
user turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
import preferences as prefs_mod
from db import async_session_factory, get_db
from models import Chat, Message, User

log = logging.getLogger("somm.chats")

router = APIRouter(dependencies=[Depends(auth.get_current_user)])

MAX_HISTORY_MESSAGES = 12  # §9 per-chat context window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _chat_dict(c: Chat) -> dict:
    return {
        "id": c.id,
        "user_id": c.user_id,
        "title": c.title,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "archived_at": c.archived_at,
    }


def _message_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "chat_id": m.chat_id,
        "role": m.role,
        "content": m.content,
        "attachments_json": m.attachments_json,
        "created_at": m.created_at,
    }


async def _load_chat_or_404(
    db: AsyncSession, chat_id: str, user: User, *, allow_admin: bool = True
) -> Chat:
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat.user_id != user.id and not (allow_admin and user.role == "admin"):
        raise HTTPException(status_code=403, detail="Not your chat")
    return chat


async def _load_recent_messages(
    db: AsyncSession, chat_id: str, limit: int = MAX_HISTORY_MESSAGES
) -> List[Message]:
    """Return the last `limit` messages in chronological order."""
    result = await db.execute(
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# Title generation (fire-and-forget)
# ---------------------------------------------------------------------------


def _extract_title(raw: str) -> str:
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    line = line.strip().strip('"').strip("'")
    # Guard against accidental long outputs.
    if len(line) > 80:
        line = line[:77].rstrip() + "…"
    return line or "New chat"


async def _generate_title_async(chat_id: str, first_question: str) -> None:
    """
    Call Claude for a short title, write it to the chat row. Opens its own
    DB session because this runs outside the request's transaction.
    Failures are swallowed — the chat remains untitled, which is harmless.
    """
    try:
        import main  # late import to avoid circular on module load

        def _call():
            return main.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=40,
                temperature=0,
                system=(
                    "You produce very short chat titles. Respond with exactly "
                    "one line, 3-7 words, no quotes, no punctuation at the "
                    "end. Summarise the user's question."
                ),
                messages=[{"role": "user", "content": first_question}],
            )

        response = await asyncio.to_thread(_call)
        title = _extract_title(response.content[0].text)

        async with async_session_factory() as db:
            chat = await db.get(Chat, chat_id)
            if chat is None or chat.title:
                return
            chat.title = title
            chat.updated_at = _iso(_now())
            await db.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[somm.chats] title generation failed for {chat_id}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# API — list / create
# ---------------------------------------------------------------------------


class CreateChatBody(BaseModel):
    title: Optional[str] = None


@router.get("/api/chats")
async def list_chats(
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's non-archived chats, newest update first."""
    result = await db.execute(
        select(Chat)
        .where(Chat.user_id == user.id)
        .where(Chat.archived_at.is_(None))
        .order_by(Chat.updated_at.desc().nullslast(), Chat.created_at.desc())
    )
    return {"chats": [_chat_dict(c) for c in result.scalars().all()]}


@router.post("/api/chats")
async def create_chat(
    body: CreateChatBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = _iso(_now())
    chat = Chat(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=body.title,
        created_at=now,
        updated_at=now,
    )
    db.add(chat)
    await db.commit()
    return _chat_dict(chat)


# ---------------------------------------------------------------------------
# API — read / rename / archive
# ---------------------------------------------------------------------------


@router.get("/api/chats/{chat_id}")
async def get_chat(
    chat_id: str,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    chat = await _load_chat_or_404(db, chat_id, user)
    result = await db.execute(
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at.asc())
    )
    msgs = result.scalars().all()
    return {
        "chat": _chat_dict(chat),
        "messages": [_message_dict(m) for m in msgs],
    }


class PatchChatBody(BaseModel):
    title: Optional[str] = None


@router.patch("/api/chats/{chat_id}")
async def rename_chat(
    chat_id: str,
    body: PatchChatBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Rename is owner-only even for admins — admins can view but not edit
    # somebody else's chat title.
    chat = await _load_chat_or_404(db, chat_id, user, allow_admin=False)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        if len(title) > 120:
            raise HTTPException(status_code=400, detail="Title too long")
        chat.title = title
        chat.updated_at = _iso(_now())
    await db.commit()
    return _chat_dict(chat)


@router.delete("/api/chats/{chat_id}")
async def archive_chat(
    chat_id: str,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete: set archived_at. The row and messages remain for audit."""
    chat = await _load_chat_or_404(db, chat_id, user, allow_admin=False)
    if chat.archived_at is None:
        chat.archived_at = _iso(_now())
        chat.updated_at = chat.archived_at
        await db.commit()
    return {"ok": True, "archived_at": chat.archived_at}


# ---------------------------------------------------------------------------
# API — post a message and get an assistant reply
# ---------------------------------------------------------------------------


class PostMessageBody(BaseModel):
    content: str


@router.post("/api/chats/{chat_id}/messages")
async def post_message(
    chat_id: str,
    body: PostMessageBody,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Only the owner can post new messages; admins read-only (§9/§10).
    chat = await _load_chat_or_404(db, chat_id, user, allow_admin=False)
    if chat.archived_at is not None:
        raise HTTPException(status_code=400, detail="Chat is archived")

    question = body.content.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Message is required")

    # Late import to avoid circular-import headaches at module load time.
    import main

    screenshots = main._get_relevant_screenshots(question)
    attached_prefixes: List[str] = []
    user_content_parts: list = []
    for img in screenshots:
        user_content_parts.append(img)
    user_content_parts.append({"type": "text", "text": question})

    # Recover which screenshot prefixes matched, for persistence.
    import re as _re
    q_lower = question.lower()
    for pattern, prefixes in main.SCREENSHOT_ROUTING.items():
        if _re.search(pattern, q_lower):
            for p in prefixes:
                if p not in attached_prefixes and len(attached_prefixes) < main.MAX_SCREENSHOTS:
                    attached_prefixes.append(p)

    # Persist the user turn.
    now = _now()
    is_first_user_turn = False
    count_result = await db.execute(
        select(Message).where(Message.chat_id == chat_id).where(Message.role == "user")
    )
    is_first_user_turn = count_result.scalars().first() is None

    user_msg = Message(
        id=str(uuid.uuid4()),
        chat_id=chat_id,
        role="user",
        content=question,
        attachments_json=json.dumps(attached_prefixes) if attached_prefixes else None,
        created_at=_iso(now),
    )
    db.add(user_msg)
    chat.updated_at = _iso(now)
    await db.commit()

    # Build the sliding 12-message context window from the DB (includes the
    # turn we just wrote, so the API sees the current question).
    history = await _load_recent_messages(db, chat_id, MAX_HISTORY_MESSAGES)

    messages_for_api: list = []
    for m in history:
        if m.id == user_msg.id:
            # Replace the last user turn's content with the structured form
            # that includes screenshots as image blocks.
            messages_for_api.append({"role": "user", "content": user_content_parts})
        else:
            messages_for_api.append({"role": m.role, "content": m.content or ""})

    # §7 preference injection. Zero-preference users get the base prompt
    # untouched (spec §11 step 4: "identical behaviour to v1").
    user_prefs = await prefs_mod.load_preferences_for(db, user.id)
    prefs_block = prefs_mod.build_preferences_block(user_prefs)
    system_prompt = main.SYSTEM_PROMPT + prefs_block

    # Call Claude with retries (mirrors /ask retry behaviour in v1 main.py).
    answer: Optional[str] = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            def _call():
                return main.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    temperature=0,
                    system=system_prompt,
                    messages=messages_for_api,
                )

            response = await asyncio.to_thread(_call)
            answer = response.content[0].text
            break
        except anthropic.RateLimitError as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 65
                print(
                    f"[somm.chats] Rate limited, retrying in {wait}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                await asyncio.sleep(wait)
            else:
                print(f"[somm.chats] Rate limit exceeded: {e}", file=sys.stderr)
                # Roll back the user turn so the UI can retry cleanly.
                await db.delete(user_msg)
                await db.commit()
                raise HTTPException(
                    status_code=503,
                    detail="The sommelier is busy right now. Please wait a minute and try again.",
                )
        except Exception as e:
            print(f"[somm.chats] Anthropic API error: {e}", file=sys.stderr)
            await db.delete(user_msg)
            await db.commit()
            raise HTTPException(
                status_code=500,
                detail="The sommelier is unavailable right now. Please try again.",
            )

    assistant_msg = Message(
        id=str(uuid.uuid4()),
        chat_id=chat_id,
        role="assistant",
        content=answer or "",
        created_at=_iso(_now()),
    )
    db.add(assistant_msg)
    chat.updated_at = _iso(_now())
    await db.commit()

    # Fire-and-forget title generation on the first user turn.
    if is_first_user_turn and not chat.title:
        asyncio.create_task(_generate_title_async(chat_id, question))

    return {
        "user_message": _message_dict(user_msg),
        "assistant_message": _message_dict(assistant_msg),
    }
