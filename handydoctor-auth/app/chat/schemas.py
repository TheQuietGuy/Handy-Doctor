"""
app/chat/schemas.py — Pydantic v2 request and response schemas for chat.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Citation(BaseModel):
    source: str
    url: str | None = None


class InterviewContext(BaseModel):
    """Answers from the guided interview. Every field is optional."""

    who: Literal["me", "child", "other"] = "me"
    age_years: float | None = Field(default=None, ge=0, le=120)
    duration: Literal["today", "1_3_days", "4_7_days", "1_3_weeks", "over_3_weeks"] | None = None
    severity: Literal["mild", "moderate", "severe"] | None = None
    red_flags: dict[str, bool] = Field(default_factory=dict, max_length=20)
    pregnant: bool | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    context: InterviewContext | None = None


class InterviewRequest(BaseModel):
    complaint: str = Field(min_length=1, max_length=4000)
    who: Literal["me", "child", "other"] = "me"
    age_years: float | None = Field(default=None, ge=0, le=120)


class InterviewQuestion(BaseModel):
    id: str
    text: str


class InterviewOut(BaseModel):
    emergency: bool
    category: str | None
    questions: list[InterviewQuestion]
    reasons: list[dict]


class CheckinRequest(BaseModel):
    status: Literal["better", "same", "worse"]
    severity: int | None = Field(default=None, ge=1, le=10)
    note: str | None = Field(default=None, max_length=1000)


class ChatMessageOut(_ORM):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    severity: str | None
    citations: list[Citation]
    created_at: datetime
    feedback: str | None = None
    care_level: str | None = None
    reasons: list[dict] = []
    context: dict | None = None

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
            feedback=getattr(msg, "feedback", None),
            care_level=_care_level(msg),
            reasons=_loads(getattr(msg, "triage_reasons", None), []),
            context=_loads(getattr(msg, "context", None), None),
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


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"] | None  # None clears existing feedback
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackOut(BaseModel):
    message_id: uuid.UUID
    feedback: str | None


def _loads(raw, default):
    import json

    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _care_level(msg) -> str | None:
    lv = getattr(msg, "care_level", None)
    if lv or msg.role != "assistant" or not msg.severity:
        return lv
    sev = msg.severity.value if hasattr(msg.severity, "value") else str(msg.severity)
    return {"green": "self_care", "amber": "gp", "red": "emergency"}.get(sev)
