"""app/notifications/worker.py — Scheduled notification jobs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.chat.models import ChatMessage, ChatSession, MessageRole, Severity
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import NotificationPrefs, User
from app.notifications.builder import build_followup_reminder, build_session_summary
from app.notifications.models import NotificationLog, NotificationStatus, NotificationType

logger = logging.getLogger(__name__)

SESSION_QUIET_MINUTES = 30
FOLLOWUP_MIN_HOURS = 48
FOLLOWUP_MAX_HOURS = 120
BATCH_SIZE = 50


def _dominant_severity(messages: list[ChatMessage]) -> str:
    sevs = [m.severity for m in messages if m.role == MessageRole.assistant and m.severity]
    if Severity.red in sevs:
        return Severity.red
    if Severity.amber in sevs:
        return Severity.amber
    return Severity.green


# ── Job 1: session summaries ──────────────────────────────────────────────────
async def run_session_summaries() -> None:
    async with AsyncSessionLocal() as db:
        cutoff = datetime.now(UTC) - timedelta(minutes=SESSION_QUIET_MINUTES)
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.message_count >= 2, ChatSession.updated_at <= cutoff)
            .options(selectinload(ChatSession.messages))
            .order_by(ChatSession.updated_at.desc())
            .limit(BATCH_SIZE)
        )
        sessions = result.scalars().all()

    for session in sessions:
        try:
            await _maybe_send_summary(session)
        except Exception:
            logger.exception("notification worker: summary error for session %s", session.id)


async def _maybe_send_summary(session: ChatSession) -> None:
    async with AsyncSessionLocal() as db:
        exists = (
            await db.execute(
                select(NotificationLog).where(
                    NotificationLog.session_id == session.id,
                    NotificationLog.notification_type == NotificationType.session_summary,
                )
            )
        ).scalar_one_or_none()
        if exists:
            return

        user = (
            await db.execute(
                select(User).where(User.id == session.user_id, User.is_active == True)  # noqa: E712
            )
        ).scalar_one_or_none()
        if not user:
            return

        prefs = (
            await db.execute(select(NotificationPrefs).where(NotificationPrefs.user_id == user.id))
        ).scalar_one_or_none()

        if not prefs or not prefs.session_summaries:
            db.add(
                NotificationLog(
                    user_id=user.id,
                    session_id=session.id,
                    notification_type=NotificationType.session_summary,
                    status=NotificationStatus.skipped,
                    error_detail="User preference disabled",
                )
            )
            await db.commit()
            return

        messages = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session.id)
                    .order_by(ChatMessage.created_at)
                )
            )
            .scalars()
            .all()
        )

        payload = build_session_summary(session, messages, user.full_name, settings.frontend_url)
        try:
            from app.email.sender import send_notification_email

            await send_notification_email(
                to=user.email, subject=payload.subject, html=payload.html, plain=payload.plain
            )
            status, error = NotificationStatus.sent, None
            logger.info("notification worker: summary sent to %s for %s", user.email, session.id)
        except Exception as exc:
            status, error = NotificationStatus.failed, str(exc)[:500]
            logger.exception("notification worker: summary failed for %s", user.email)

        db.add(
            NotificationLog(
                user_id=user.id,
                session_id=session.id,
                notification_type=NotificationType.session_summary,
                status=status,
                error_detail=error,
            )
        )
        await db.commit()


# ── Job 2: follow-up reminders ────────────────────────────────────────────────
async def run_followup_reminders() -> None:
    async with AsyncSessionLocal() as db:
        now = datetime.now(UTC)
        min_age = now - timedelta(hours=FOLLOWUP_MIN_HOURS)
        max_age = now - timedelta(hours=FOLLOWUP_MAX_HOURS)
        result = await db.execute(
            select(ChatSession)
            .where(
                ChatSession.message_count >= 2,
                ChatSession.updated_at <= min_age,
                ChatSession.updated_at >= max_age,
            )
            .options(selectinload(ChatSession.messages))
            .order_by(ChatSession.updated_at.desc())
            .limit(BATCH_SIZE)
        )
        sessions = result.scalars().all()

    for session in sessions:
        try:
            await _maybe_send_followup(session)
        except Exception:
            logger.exception("notification worker: followup error for session %s", session.id)


async def _maybe_send_followup(session: ChatSession) -> None:
    async with AsyncSessionLocal() as db:
        exists = (
            await db.execute(
                select(NotificationLog).where(
                    NotificationLog.session_id == session.id,
                    NotificationLog.notification_type == NotificationType.followup_reminder,
                )
            )
        ).scalar_one_or_none()
        if exists:
            return

        messages = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session.id)
                    .order_by(ChatMessage.created_at)
                )
            )
            .scalars()
            .all()
        )

        dominant_sev = _dominant_severity(messages)
        if dominant_sev == Severity.red:
            db.add(
                NotificationLog(
                    user_id=session.user_id,
                    session_id=session.id,
                    notification_type=NotificationType.followup_reminder,
                    status=NotificationStatus.skipped,
                    error_detail="Red severity — no follow-up",
                )
            )
            await db.commit()
            return

        user = (
            await db.execute(
                select(User).where(User.id == session.user_id, User.is_active == True)  # noqa: E712
            )
        ).scalar_one_or_none()
        if not user:
            return

        prefs = (
            await db.execute(select(NotificationPrefs).where(NotificationPrefs.user_id == user.id))
        ).scalar_one_or_none()

        if not prefs or not prefs.followup_reminders:
            db.add(
                NotificationLog(
                    user_id=user.id,
                    session_id=session.id,
                    notification_type=NotificationType.followup_reminder,
                    status=NotificationStatus.skipped,
                    error_detail="User preference disabled",
                )
            )
            await db.commit()
            return

        # Skip if user has started a newer session since
        newer = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.user_id == session.user_id,
                    ChatSession.id != session.id,
                    ChatSession.updated_at > session.updated_at,
                    ChatSession.message_count >= 2,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if newer:
            db.add(
                NotificationLog(
                    user_id=user.id,
                    session_id=session.id,
                    notification_type=NotificationType.followup_reminder,
                    status=NotificationStatus.skipped,
                    error_detail="User has newer session",
                )
            )
            await db.commit()
            return

        now = datetime.now(UTC)
        updated_at = session.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        hours_since = int((now - updated_at).total_seconds() / 3600)

        payload = build_followup_reminder(
            session, messages, user.full_name, settings.frontend_url, hours_since
        )
        try:
            from app.email.sender import send_notification_email

            await send_notification_email(
                to=user.email, subject=payload.subject, html=payload.html, plain=payload.plain
            )
            status, error = NotificationStatus.sent, None
            logger.info("notification worker: followup sent to %s for %s", user.email, session.id)
        except Exception as exc:
            status, error = NotificationStatus.failed, str(exc)[:500]
            logger.exception("notification worker: followup failed for %s", user.email)

        db.add(
            NotificationLog(
                user_id=user.id,
                session_id=session.id,
                notification_type=NotificationType.followup_reminder,
                status=status,
                error_detail=error,
            )
        )
        await db.commit()
