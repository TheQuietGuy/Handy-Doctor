"""
tests/test_notifications.py — Tests for the notification worker.

Coverage
────────
• NotificationLog model (basic persistence)
• builder.py — session summary and follow-up payloads
• worker.py  — deduplication, preference gating, severity filtering,
               newer-session skip, successful send, failed send
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.password import hash_password
from app.chat.models import ChatMessage, ChatSession, MessageRole, Severity
from app.database import Base
from app.models import AuthProvider, NotificationPrefs, User
from app.notifications import worker as notif_worker
from app.notifications.builder import (
    build_followup_reminder,
    build_session_summary,
)
from app.notifications.models import (
    NotificationLog,
    NotificationStatus,
    NotificationType,
)

# ── In-memory SQLite test DB ───────────────────────────────────────────────────
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    notif_worker.AsyncSessionLocal = TestSession
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db():
    async with TestSession() as session:
        yield session
        await session.rollback()


# ── Shared helpers ────────────────────────────────────────────────────────────
async def _make_user(
    db: AsyncSession,
    email: str = "notif@example.com",
    summaries: bool = True,
    followups: bool = True,
) -> User:
    user = User(
        email=email,
        full_name="Notif User",
        password_hash=hash_password("Test1234!"),
        auth_provider=AuthProvider.local,
        is_email_verified=True,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(
        NotificationPrefs(
            user_id=user.id,
            session_summaries=summaries,
            followup_reminders=followups,
            product_updates=False,
            security_alerts=True,
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _make_session(
    db: AsyncSession,
    user: User,
    title: str = "Sore throat",
    hours_ago: float = 1.0,
    severity: str = Severity.green,
) -> ChatSession:
    updated_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    session = ChatSession(
        user_id=user.id,
        title=title,
        message_count=2,
        created_at=updated_at,
        updated_at=updated_at,
    )
    db.add(session)
    await db.flush()

    # Add user + assistant message pair
    db.add(
        ChatMessage(
            session_id=session.id,
            role=MessageRole.user,
            content="I have a sore throat",
            created_at=updated_at,
        )
    )
    db.add(
        ChatMessage(
            session_id=session.id,
            role=MessageRole.assistant,
            content="Based on NHS guidelines, rest and fluids are recommended.",
            severity=severity,
            citations=json.dumps(
                [{"source": "NHS: Sore throat", "url": "https://nhs.uk/sore-throat"}]
            ),
            created_at=updated_at,
        )
    )
    await db.commit()
    await db.refresh(session)
    return session


# ════════════════════════════════════════════════════════════════════════════
# 1. MODEL — NotificationLog
# ════════════════════════════════════════════════════════════════════════════
class TestNotificationLogModel:
    @pytest.mark.asyncio
    async def test_create_sent_log(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user)
        log = NotificationLog(
            user_id=user.id,
            session_id=session.id,
            notification_type=NotificationType.session_summary,
            status=NotificationStatus.sent,
        )
        db.add(log)
        await db.commit()
        result = await db.execute(select(NotificationLog).where(NotificationLog.user_id == user.id))
        fetched = result.scalar_one()
        assert fetched.notification_type == NotificationType.session_summary
        assert fetched.status == NotificationStatus.sent
        assert fetched.sent_at is not None

    @pytest.mark.asyncio
    async def test_create_failed_log_with_detail(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user)
        log = NotificationLog(
            user_id=user.id,
            session_id=session.id,
            notification_type=NotificationType.followup_reminder,
            status=NotificationStatus.failed,
            error_detail="SMTP timeout",
        )
        db.add(log)
        await db.commit()
        result = await db.execute(select(NotificationLog).where(NotificationLog.user_id == user.id))
        fetched = result.scalar_one()
        assert fetched.status == NotificationStatus.failed
        assert fetched.error_detail == "SMTP timeout"

    @pytest.mark.asyncio
    async def test_log_survives_null_user(self, db):
        """After GDPR erasure user_id goes NULL — log must persist."""
        log = NotificationLog(
            user_id=None,
            session_id=None,
            notification_type=NotificationType.session_summary,
            status=NotificationStatus.sent,
        )
        db.add(log)
        await db.commit()
        result = await db.execute(select(NotificationLog))
        assert result.scalar_one_or_none() is not None


# ════════════════════════════════════════════════════════════════════════════
# 2. BUILDER — email content
# ════════════════════════════════════════════════════════════════════════════
class TestNotificationBuilder:
    @pytest.mark.asyncio
    async def test_session_summary_has_correct_subject(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user, title="Persistent headache")
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
        payload = build_session_summary(session, msgs, user.full_name, "http://app.test")
        assert "Persistent headache" in payload.subject
        assert "summary" in payload.subject.lower()

    @pytest.mark.asyncio
    async def test_session_summary_html_contains_severity(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user, severity=Severity.amber)
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
        payload = build_session_summary(session, msgs, user.full_name, "http://app.test")
        assert "GP" in payload.html or "amber" in payload.html.lower()

    @pytest.mark.asyncio
    async def test_session_summary_html_contains_citation(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user)
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
        payload = build_session_summary(session, msgs, user.full_name, "http://app.test")
        assert "NHS" in payload.html
        assert "NHS" in payload.plain

    @pytest.mark.asyncio
    async def test_session_summary_plain_text_present(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user)
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
        payload = build_session_summary(session, msgs, user.full_name, "http://app.test")
        assert len(payload.plain) > 100
        assert "substitute" in payload.plain.lower()

    @pytest.mark.asyncio
    async def test_followup_reminder_green_severity(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user, severity=Severity.green)
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
        payload = build_followup_reminder(session, msgs, user.full_name, "http://app.test", 48)
        assert "48 hours" in payload.html
        assert session.title in payload.subject
        # Green → self-care copy
        assert "self-care" in payload.html.lower() or "GP" in payload.html

    @pytest.mark.asyncio
    async def test_followup_reminder_amber_severity(self, db):
        user = await _make_user(db)
        session = await _make_session(db, user, severity=Severity.amber)
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
        payload = build_followup_reminder(session, msgs, user.full_name, "http://app.test", 52)
        # Amber → "see a GP" / "urgent" copy
        assert "GP" in payload.html or "urgent" in payload.html.lower()

    @pytest.mark.asyncio
    async def test_builder_handles_empty_citations(self, db):
        """Sessions with no citations in messages should not crash."""
        user = await _make_user(db)
        session = await _make_session(db, user)
        # Override citations to empty
        async with TestSession() as s:
            msgs = (
                (await s.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)))
                .scalars()
                .all()
            )
            for m in msgs:
                m.citations = "[]"
        payload = build_session_summary(session, msgs, user.full_name, "http://app.test")
        assert payload.html  # must not crash


# ════════════════════════════════════════════════════════════════════════════
# 3. WORKER — session summaries
# ════════════════════════════════════════════════════════════════════════════
class TestSessionSummaryWorker:
    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_sends_summary_for_quiet_session(self, mock_send, db):
        user = await _make_user(db)
        session = await _make_session(db, user, hours_ago=1.0)
        # Session is 60 minutes old — qualifies (quiet >= 30 min)

        await notif_worker.run_session_summaries()

        mock_send.assert_awaited_once()
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["to"] == user.email
        assert session.title in call_kwargs["subject"]

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_does_not_send_twice(self, mock_send, db):
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=1.0)

        await notif_worker.run_session_summaries()
        await notif_worker.run_session_summaries()

        assert mock_send.await_count == 1  # deduplicated

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_recent_session(self, mock_send, db):
        user = await _make_user(db)
        # Session updated only 10 minutes ago — not quiet yet
        await _make_session(db, user, hours_ago=0.15)

        await notif_worker.run_session_summaries()

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_when_preference_disabled(self, mock_send, db):
        user = await _make_user(db, summaries=False)
        await _make_session(db, user, hours_ago=1.0)

        await notif_worker.run_session_summaries()

        mock_send.assert_not_awaited()

        # A 'skipped' log must be written for deduplication
        async with TestSession() as s:
            result = await s.execute(select(NotificationLog))
            log = result.scalar_one()
        assert log.status == NotificationStatus.skipped

    @pytest.mark.asyncio
    @patch(
        "app.email.sender.send_notification_email",
        new_callable=AsyncMock,
        side_effect=Exception("SMTP down"),
    )
    async def test_records_failed_status_on_send_error(self, mock_send, db):
        user = await _make_user(db)
        session = await _make_session(db, user, hours_ago=1.0)

        await notif_worker.run_session_summaries()

        async with TestSession() as s:
            result = await s.execute(
                select(NotificationLog).where(NotificationLog.session_id == session.id)
            )
            log = result.scalar_one()
        assert log.status == NotificationStatus.failed
        assert "SMTP down" in log.error_detail

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_processes_multiple_sessions(self, mock_send, db):
        user = await _make_user(db)
        for i in range(3):
            await _make_session(db, user, hours_ago=1.0, title=f"Session {i}")
        await notif_worker.run_session_summaries()
        assert mock_send.await_count == 3


# ════════════════════════════════════════════════════════════════════════════
# 4. WORKER — follow-up reminders
# ════════════════════════════════════════════════════════════════════════════
class TestFollowupReminderWorker:
    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_sends_followup_for_amber_session(self, mock_send, db):
        user = await _make_user(db)
        session = await _make_session(db, user, hours_ago=50, severity=Severity.amber)

        await notif_worker.run_followup_reminders()

        mock_send.assert_awaited_once()
        assert session.title in mock_send.call_args[1]["subject"]

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_sends_followup_for_green_session(self, mock_send, db):
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=50, severity=Severity.green)
        await notif_worker.run_followup_reminders()
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_red_severity_session(self, mock_send, db):
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=50, severity=Severity.red)
        await notif_worker.run_followup_reminders()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_session_too_recent(self, mock_send, db):
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=24, severity=Severity.amber)
        await notif_worker.run_followup_reminders()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_session_too_old(self, mock_send, db):
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=130, severity=Severity.amber)
        await notif_worker.run_followup_reminders()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_when_preference_disabled(self, mock_send, db):
        user = await _make_user(db, followups=False)
        await _make_session(db, user, hours_ago=50, severity=Severity.amber)
        await notif_worker.run_followup_reminders()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_skips_when_user_has_newer_session(self, mock_send, db):
        user = await _make_user(db)
        # Old session (49 hours ago)
        await _make_session(db, user, title="Old session", hours_ago=49, severity=Severity.amber)
        # Newer session (2 hours ago) — user has already consulted since
        await _make_session(db, user, title="Newer session", hours_ago=2)

        await notif_worker.run_followup_reminders()

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_does_not_send_followup_twice(self, mock_send, db):
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=50, severity=Severity.amber)

        await notif_worker.run_followup_reminders()
        await notif_worker.run_followup_reminders()

        assert mock_send.await_count == 1

    @pytest.mark.asyncio
    @patch(
        "app.email.sender.send_notification_email",
        new_callable=AsyncMock,
        side_effect=Exception("Resend timeout"),
    )
    async def test_records_failed_on_send_error(self, mock_send, db):
        user = await _make_user(db)
        session = await _make_session(db, user, hours_ago=50, severity=Severity.amber)

        await notif_worker.run_followup_reminders()

        async with TestSession() as s:
            result = await s.execute(
                select(NotificationLog).where(NotificationLog.session_id == session.id)
            )
            log = result.scalar_one()
        assert log.status == NotificationStatus.failed
        assert "Resend timeout" in log.error_detail


# ════════════════════════════════════════════════════════════════════════════
# 5. COMBINED — summary then follow-up
# ════════════════════════════════════════════════════════════════════════════
class TestCombinedNotifications:
    @pytest.mark.asyncio
    @patch("app.email.sender.send_notification_email", new_callable=AsyncMock)
    async def test_session_gets_both_summary_and_followup(self, mock_send, db):
        """
        A session that's 50 hours old should receive:
          1. A session summary (already quiet for 30+ minutes)
          2. A follow-up reminder (48–120 hours old, green/amber severity)
        """
        user = await _make_user(db)
        await _make_session(db, user, hours_ago=50, severity=Severity.green)

        await notif_worker.run_session_summaries()
        await notif_worker.run_followup_reminders()

        assert mock_send.await_count == 2
        subjects = [call[1]["subject"] for call in mock_send.call_args_list]
        assert any("summary" in s.lower() for s in subjects)
        assert any("follow" in s.lower() for s in subjects)
