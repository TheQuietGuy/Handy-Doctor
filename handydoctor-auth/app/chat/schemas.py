"""
app/chat/schemas.py — Pydantic v2 request and response schemas for chat.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Citation(BaseModel):
    source: str
    url: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ChatMessageOut(_ORM):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    severity: str | None
    citations: list[Citation]
    created_at: datetime

    @classmethod
    def from_orm_with_citations(cls, msg) -> "ChatMessageOut":
        import json

        raw = msg.citations or "[]"
        try:
            cits = [Citation(**c) for c in json.loads(raw)]
        except Exception:
            cits = []
        return cls(
            id=msg.id,
            session_id=msg.session_id,
            role=msg.role,
            content=msg.content,
            severity=msg.severity,
            citations=cits,
            created_at=msg.created_at,
        )


class ChatSessionOut(_ORM):
    id: uuid.UUID
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class ChatSessionDetailOut(_ORM):
    id: uuid.UUID
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageOut] = []

    @classmethod
    def from_session(cls, session) -> "ChatSessionDetailOut":
        return cls(
            id=session.id,
            title=session.title,
            message_count=session.message_count,
            created_at=session.created_at,
            updated_at=session.updated_at,
            messages=[ChatMessageOut.from_orm_with_citations(m) for m in session.messages],
        )


class SendMessageResponse(BaseModel):
    user_message: ChatMessageOut
    assistant_message: ChatMessageOut
    session_id: uuid.UUID


class SessionListResponse(BaseModel):
    sessions: list[ChatSessionOut]
    total: int
