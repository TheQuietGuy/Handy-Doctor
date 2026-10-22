"""
app/chat/models.py — ChatSession and ChatMessage ORM models.

Schema design decisions
────────────────────────
• sessions.title  — auto-generated from the first user message (≤60 chars).
  Never stored as NULL; falls back to "Chat {date}" if generation fails.

• messages.role   — 'user' | 'assistant'.  We store both so the full
  conversation history can be replayed into the LLM on each turn.

• messages.severity — 'green' | 'amber' | 'red' | NULL.
  Only set on assistant messages.  Drives the triage badge in the UI.

• messages.citations — JSON array of {source, url} dicts.
  Stored as Text (JSON string) to stay compatible with SQLite in tests.

• sessions.message_count — denormalised counter kept in sync by the
  service layer.  Avoids a COUNT(*) on every session-list query.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class MessageRole(enum.StrEnum):
    user = "user"
    assistant = "assistant"


class Severity(enum.StrEnum):
    green = "green"  # self-care at home
    amber = "amber"  # see a GP soon
    red = "red"  # call 999 / A&E


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="New chat")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
        passive_deletes=True,
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Assistant-only fields
    severity: Mapped[Severity | None] = mapped_column(Enum(Severity), nullable=True)
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
