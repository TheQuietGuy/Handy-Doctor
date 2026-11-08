"""
app/chat/router.py — Chat session and message endpoints.

Prefix: /api/chat

All routes require a verified, active user (get_verified_user dependency).

Endpoint map
────────────────────────────────────────────────────
POST   /sessions              Create a new chat session
GET    /sessions              List all sessions (paginated)
GET    /sessions/{id}         Get session with full message history
POST   /sessions/{id}/messages Send a user message → get AI reply
DELETE /sessions/{id}         Delete a session and all messages
GET    /sessions/{id}/export  Export session as JSON
────────────────────────────────────────────────────
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_verified_user
from app.chat import service
from app.chat.ai.interview import plan as interview_plan
from app.chat.models import ChatMessage, ChatSession, MessageRole
from app.chat.schemas import (
    ChatMessageOut,
    ChatSessionDetailOut,
    ChatSessionOut,
    CheckinRequest,
    FeedbackOut,
    FeedbackRequest,
    InterviewOut,
    InterviewRequest,
    SendMessageRequest,
    SendMessageResponse,
    SessionListResponse,
)
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post(
    "/sessions",
    response_model=ChatSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> ChatSessionOut:
    """Create a new empty chat session for the current user."""
    session = await service.create_session(db, current_user.id)
    return ChatSessionOut.model_validate(session)


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> SessionListResponse:
    """Return a paginated list of the user's sessions, most recent first."""
    sessions, total = await service.list_sessions(db, current_user.id, offset, limit)
    return SessionListResponse(
        sessions=[ChatSessionOut.model_validate(s) for s in sessions],
        total=total,
    )


@router.get("/sessions/{session_id}", response_model=ChatSessionDetailOut)
async def get_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetailOut:
    """Return a session with its full message history."""
    session = await service.get_session(db, session_id, current_user.id, with_messages=True)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    return ChatSessionDetailOut.from_session(session)


@router.post("/sessions/{session_id}/messages", response_model=SendMessageResponse)
async def send_message(
    session_id: uuid.UUID,
    body: SendMessageRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> SendMessageResponse:
    """
    Send a user message.  The RAG pipeline runs synchronously and the
    AI reply is returned in the same response.
    """
    ctx = body.context.model_dump(exclude_none=True) if body.context else None
    result = await service.add_message(db, session_id, current_user.id, body.content, ctx)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")

    user_msg, assistant_msg = result
    return SendMessageResponse(
        user_message=ChatMessageOut.from_orm_with_citations(user_msg),
        assistant_message=ChatMessageOut.from_orm_with_citations(assistant_msg),
        session_id=session_id,
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a session and all its messages."""
    deleted = await service.delete_session(db, session_id, current_user.id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Export a full session as a portable JSON object."""
    export = await service.export_session_json(db, session_id, current_user.id)
    if export is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    return export


@router.post("/messages/{message_id}/feedback", response_model=FeedbackOut)
async def give_feedback(
    message_id: uuid.UUID,
    body: FeedbackRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> FeedbackOut:
    """Rate an assistant answer (thumbs up / down) to help improve answer quality."""
    msg = await db.get(ChatMessage, message_id)
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found.")
    session = await db.get(ChatSession, msg.session_id)
    if session is None or session.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found.")
    if msg.role != MessageRole.assistant:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only assistant answers can be rated.")
    msg.feedback = body.rating
    msg.feedback_comment = (body.comment or None) if body.rating else None
    await db.flush()
    return FeedbackOut(message_id=msg.id, feedback=msg.feedback)


@router.post("/interview", response_model=InterviewOut)
async def interview(
    body: InterviewRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
) -> InterviewOut:
    """
    Questions to ask before the first answer, chosen from the complaint.
    If the complaint already contains emergency signs, `emergency` is true and
    the client should send the message straight away instead of asking.
    """
    return InterviewOut(**interview_plan(body.complaint, body.age_years, body.who))


@router.post("/sessions/{session_id}/checkin", response_model=SendMessageResponse)
async def checkin(
    session_id: uuid.UUID,
    body: CheckinRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> SendMessageResponse:
    """Record how the user feels now (better / same / worse) and re-triage."""
    result = await service.checkin(db, session_id, current_user.id, body.status, body.severity, body.note)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    user_msg, assistant_msg = result
    return SendMessageResponse(
        user_message=ChatMessageOut.from_orm_with_citations(user_msg),
        assistant_message=ChatMessageOut.from_orm_with_citations(assistant_msg),
        session_id=session_id,
    )
