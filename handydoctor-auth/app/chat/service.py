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
    context: dict | None = None,
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
        context=json.dumps(context) if context else None,
    )
    db.add(user_msg)
    await db.flush()

    # Auto-title on the first user message
    if session.message_count == 0:
        session.title = _auto_title(content)

    # Interview answers from earlier in this check still apply to follow-ups
    if context is None:
        for m in session.messages:
            if m.role == MessageRole.user and m.context:
                context = json.loads(m.context)
                break

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
        context=context,
    )

    # ── Assistant message ────────────────────────────────────────────────
    citations_json = json.dumps([{"source": c.source, "url": c.url} for c in ai_response.citations])
    assistant_msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.assistant,
        content=ai_response.content,
        severity=Severity(ai_response.severity),
        citations=citations_json,
        care_level=ai_response.care_level,
        triage_reasons=json.dumps(ai_response.reasons),
    )
    db.add(assistant_msg)

    # ── Update session counters ──────────────────────────────────────────
    session.message_count += 2  # user + assistant
    session.updated_at = datetime.now(UTC)

    await db.flush()
    return user_msg, assistant_msg


# ── Follow-up check-in ────────────────────────────────────────────────────────
_CHECKIN_TEXT = {
    "better": "Good to hear you're feeling better. Keep following the self-care advice, and finish any "
              "treatment you've been given.",
    "same": "Thanks for checking in. As things haven't improved, it's worth getting some advice.",
    "worse": "Thanks for letting us know. Because your symptoms are getting worse, please get help sooner "
             "than we suggested before.",
}


async def checkin(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str,
    severity: int | None = None,
    note: str | None = None,
) -> tuple[ChatMessage, ChatMessage] | None:
    """
    Record a "how are you now?" check-in and re-triage.

    * better -> stays at the rule floor for anything new in the note
    * same   -> one level up (self-care -> pharmacist -> GP), never below the previous level
    * worse  -> one level up from the previous advice, and at least "contact your GP"
    Red-flag rules over the note can raise it further.  A severity score is also
    written to the symptom journal so the trend chart reflects it.
    """
    from app.chat.ai import care
    from app.chat.ai.redflags import evaluate
    from app.journal.models import SymptomEntry

    session = await get_session(db, session_id, user_id, with_messages=True)
    if session is None:
        return None

    prev = "self_care"
    for m in reversed(session.messages):
        if m.role == MessageRole.assistant:
            prev = m.care_level or {"green": "self_care", "amber": "gp", "red": "emergency"}.get(
                m.severity.value if m.severity else "green", "self_care")
            break

    rules = evaluate(note or "")
    if status == "better":
        level = rules.level
    elif status == "same":
        level = care.highest(prev, care.raise_by(prev) if care.rank(prev) < care.rank("gp") else prev)
    else:
        level = care.highest(care.raise_by(prev), "gp")
    level = care.highest(level, rules.level)
    lv = care.LEVELS[level]

    label = {"better": "Feeling better", "same": "About the same", "worse": "Feeling worse"}[status]
    user_text = f"Check-in: {label}" + (f" ({severity}/10)" if severity else "") + (f". {note.strip()}" if note else "")
    reasons = list(rules.reasons)
    if status != "better":
        reasons.insert(0, {"id": "checkin", "level": level, "source": "HandyDoctor follow-up rule",
                           "reason": f"You said you're {label.lower()} since your last check."})
    body = f"**{lv.label}.** {_CHECKIN_TEXT[status]} {lv.action}"
    if status == "better":
        body += "\n\nIf anything new or worrying starts, do another check or call NHS 111."
    body += "\n\n⚕️ This is not a substitute for professional medical advice."

    user_msg = ChatMessage(session_id=session_id, role=MessageRole.user, content=user_text)
    db.add(user_msg)
    await db.flush()
    assistant_msg = ChatMessage(
        session_id=session_id, role=MessageRole.assistant, content=body,
        severity=Severity(lv.severity), citations="[]", care_level=level,
        triage_reasons=json.dumps(reasons),
    )
    db.add(assistant_msg)
    if severity:
        db.add(SymptomEntry(
            user_id=user_id, symptom=(session.title or "Symptoms")[:120], severity=severity,
            notes=f"Check-in: {label.lower()}" + (f". {note.strip()}" if note else ""),
            recorded_at=datetime.now(UTC),
        ))
    session.message_count += 2
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
                "care_level": m.care_level,
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
