"""
app/chat/service.py — Business logic for chat sessions and messages.

Responsibilities
────────────────
• create_session        Create a new ChatSession for a user.
• get_session           Fetch a session (with ownership check).
• list_sessions         Paginated list for the past-chats page.
• add_message           Persist a user message, run the RAG pipeline,
                        persist the assistant reply, increment counters.
• delete_session        Hard-delete a session and all its messages.
• export_session_json   Build a portable JSON export of the conversation.
• _auto_title           Derive a meaningful title from the first message.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.chat.ai.pipeline import AIResponse, run_pipeline
from app.chat.ai.retrieval import MockRetriever
from app.chat.models import ChatMessage, ChatSession, MessageRole, Severity


# ── Session CRUD ──────────────────────────────────────────────────────────────
async def create_session(db: AsyncSession, user_id: uuid.UUID) -> ChatSession:
    session = ChatSession(user_id=user_id, title="New chat", message_count=0)
    db.add(session)
    await db.flush()
    return session


async def get_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    with_messages: bool = False,
) -> ChatSession | None:
    """
    Fetch a session and verify ownership.  Returns None if not found
    or the session belongs to another user (treat identically — no info leak).
    """
    stmt = select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.user_id == user_id,
    )
    if with_messages:
        stmt = stmt.options(selectinload(ChatSession.messages))

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[ChatSession], int]:
    """Return (sessions_page, total_count) ordered by most recently updated."""
    count_stmt = select(func.count()).where(ChatSession.user_id == user_id)
    total: int = (await db.execute(count_stmt)).scalar_one()

    list_stmt = (
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(desc(ChatSession.updated_at))
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(list_stmt)
    return result.scalars().all(), total


async def delete_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Delete a session.  Returns False if not found or not owned by user."""
    session = await get_session(db, session_id, user_id)
    if session is None:
        return False
    await db.delete(session)
    return True


# ── Messaging ─────────────────────────────────────────────────────────────────
async def add_message(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
) -> tuple[ChatMessage, ChatMessage] | None:
    """
    Persist the user message, run the RAG pipeline, persist the AI reply.

    Returns (user_message, assistant_message) or None if session not found.
    """
    session = await get_session(db, session_id, user_id, with_messages=True)
    if session is None:
        return None

    # ── User message ─────────────────────────────────────────────────────
    user_msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.user,
        content=content,
    )
    db.add(user_msg)
    await db.flush()

    # Auto-title on the first user message
    if session.message_count == 0:
        session.title = _auto_title(content)

    # ── Build history for the LLM ────────────────────────────────────────
    history = [
        {"role": m.role.value, "content": m.content}
        for m in session.messages
        if m.id != user_msg.id  # exclude the message we just added
    ]

    # ── Run RAG pipeline ─────────────────────────────────────────────────
    ai_response: AIResponse = await run_pipeline(
        query=content,
        history=history,
        retriever=MockRetriever(),  # swap for ChromaRetriever() when ready
    )

    # ── Assistant message ────────────────────────────────────────────────
    citations_json = json.dumps([{"source": c.source, "url": c.url} for c in ai_response.citations])
    assistant_msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.assistant,
        content=ai_response.content,
        severity=Severity(ai_response.severity),
        citations=citations_json,
    )
    db.add(assistant_msg)

    # ── Update session counters ──────────────────────────────────────────
    session.message_count += 2  # user + assistant
    session.updated_at = datetime.now(UTC)

    await db.flush()
    return user_msg, assistant_msg


# ── Export ────────────────────────────────────────────────────────────────────
async def export_session_json(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict | None:
    session = await get_session(db, session_id, user_id, with_messages=True)
    if session is None:
        return None

    return {
        "export_format": "handydoctor_chat_v1",
        "exported_at": datetime.now(UTC).isoformat(),
        "session": {
            "id": str(session.id),
            "title": session.title,
            "created_at": session.created_at.isoformat(),
            "message_count": session.message_count,
        },
        "messages": [
            {
                "role": m.role.value,
                "content": m.content,
                "severity": m.severity,
                "citations": json.loads(m.citations or "[]"),
                "created_at": m.created_at.isoformat(),
            }
            for m in session.messages
        ],
        "disclaimer": (
            "This conversation was generated by HandyDoctor, an AI triage assistant. "
            "It is not a substitute for professional medical advice, diagnosis, or treatment."
        ),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────
def _auto_title(first_message: str, max_len: int = 60) -> str:
    """
    Derive a session title from the first user message.
    Strips common filler phrases, capitalises, and truncates.
    """
    _STRIP = [
        "i have ",
        "i've got ",
        "i've been ",
        "i am ",
        "i'm ",
        "can you help me with ",
        "my ",
        "hello ",
        "hi ",
    ]
    text = first_message.strip()
    lower = text.lower()
    for prefix in _STRIP:
        if lower.startswith(prefix):
            text = text[len(prefix) :]
            break

    text = text[:max_len].strip()
    if not text:
        return "Chat " + datetime.now(UTC).strftime("%d %b %Y")

    return text[0].upper() + text[1:]
