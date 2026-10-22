"""
tests/test_gdpr.py — Tests for the GDPR deletion worker and data export.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit import AuditEvent, AuditLog
from app.auth.password import hash_password
from app.database import Base, get_db, get_redis
from app.gdpr.worker import _wipe_user, build_export_payload, run_pending_deletions
from app.models import (
    AccountDeletion,
    AuthProvider,
    NotificationPrefs,
    RefreshToken,
    User,
)
from main import app

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)


async def override_db():
    async with TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def make_redis_mock():
    from unittest.mock import MagicMock

    mock = AsyncMock()
    mock.exists = AsyncMock(return_value=0)
    mock.incr = AsyncMock(return_value=1)
    mock.expire = AsyncMock(return_value=True)
    mock.set = AsyncMock(return_value=True)
    mock.get = AsyncMock(return_value=None)
    mock.delete = AsyncMock(return_value=1)
    mock.ttl = AsyncMock(return_value=-2)
    mock.pipeline = MagicMock(
        return_value=AsyncMock(
            execute=AsyncMock(return_value=[1, True]),
            incr=MagicMock(),
            expire=MagicMock(),
            delete=MagicMock(),
        )
    )
    return mock


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Patch the worker's AsyncSessionLocal to use the test DB
    from app.gdpr import worker as gdpr_worker

    gdpr_worker.AsyncSessionLocal = TestSession
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: make_redis_mock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    async with TestSession() as session:
        yield session
        await session.rollback()


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _make_user(
    db: AsyncSession,
    email: str = "victim@example.com",
    verified: bool = True,
) -> User:
    user = User(
        email=email,
        full_name="Jane Victim",
        password_hash=hash_password("Test1234!"),
        auth_provider=AuthProvider.local,
        is_email_verified=verified,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # materialise user.id before FK ref
    db.add(NotificationPrefs(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def _queue_deletion(
    db: AsyncSession, user: User, grace_expired: bool = True
) -> AccountDeletion:
    offset = -timedelta(seconds=1) if grace_expired else timedelta(days=14)
    deletion = AccountDeletion(
        user_id=user.id,
        execute_after=datetime.now(UTC) + offset,
    )
    db.add(deletion)
    await db.commit()
    await db.refresh(deletion)
    return deletion


# ════════════════════════════════════════════════════════════════════════════
# DATA EXPORT
# ════════════════════════════════════════════════════════════════════════════
class TestDataExport:
    @pytest.mark.asyncio
    async def test_export_payload_structure(self, db):
        user = await _make_user(db)
        payload = await build_export_payload(db, user)

        # Top-level keys
        assert "export_generated_at" in payload
        assert "data_controller" in payload
        assert "gdpr_basis" in payload

        # Account section
        acct = payload["account"]
        assert acct["email"] == user.email
        assert acct["full_name"] == user.full_name
        assert str(user.id) == acct["id"]

        # Notification prefs present (auto-created)
        assert payload["notification_preferences"] is not None
        assert "session_summaries" in payload["notification_preferences"]

        # No active sessions at creation
        assert payload["active_sessions"] == []

        # No deletion request yet
        assert payload["deletion_request"] is None

    @pytest.mark.asyncio
    async def test_export_includes_sessions(self, db):
        user = await _make_user(db)
        # Add a fake refresh token
        db.add(
            RefreshToken(
                user_id=user.id,
                token_hash="fakehash123",
                device_hint="Chrome/Mac",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await db.commit()

        payload = await build_export_payload(db, user)
        assert len(payload["active_sessions"]) == 1
        assert payload["active_sessions"][0]["device_hint"] == "Chrome/Mac"

    @pytest.mark.asyncio
    async def test_export_includes_pending_deletion(self, db):
        user = await _make_user(db)
        await _queue_deletion(db, user, grace_expired=False)
        payload = await build_export_payload(db, user)

        assert payload["deletion_request"] is not None
        assert "execute_after" in payload["deletion_request"]

    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_data_export_email", new_callable=AsyncMock)
    async def test_export_endpoint(self, mock_email, client):
        """POST /me/export should trigger export email and return 200."""
        async with TestSession() as db:
            await _make_user(db)

        # Login
        resp = await client.post(
            "/api/accounts/login",
            json={
                "email": "victim@example.com",
                "password": "Test1234!",
            },
        )
        token = resp.json()["access_token"]

        resp = await client.post(
            "/api/accounts/me/export",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert "sent to" in resp.json()["message"]


# ════════════════════════════════════════════════════════════════════════════
# WIPE LOGIC
# ════════════════════════════════════════════════════════════════════════════
class TestWipeUser:
    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_wipe_anonymises_user(self, mock_complete, mock_export, db):
        user = await _make_user(db)
        deletion = await _queue_deletion(db, user)
        original_email = user.email

        await _wipe_user(user.id, deletion.id)

        async with TestSession() as check_db:
            result = await check_db.execute(select(User).where(User.id == user.id))
            wiped = result.scalar_one()

        # PII must be removed
        assert wiped.email != original_email
        assert "deleted-" in wiped.email
        assert ".invalid" in wiped.email
        assert wiped.full_name == "[deleted]"
        assert wiped.password_hash is None
        assert wiped.google_sub is None
        assert wiped.date_of_birth is None
        assert wiped.country is None
        assert wiped.avatar_url is None
        assert wiped.is_active is False

    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_wipe_deletes_child_rows(self, mock_complete, mock_export, db):
        user = await _make_user(db)
        deletion = await _queue_deletion(db, user)

        # Add a refresh token
        db.add(
            RefreshToken(
                user_id=user.id,
                token_hash="testhash",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await db.commit()

        await _wipe_user(user.id, deletion.id)

        async with TestSession() as check_db:
            rt_result = await check_db.execute(
                select(RefreshToken).where(RefreshToken.user_id == user.id)
            )
            assert rt_result.scalar_one_or_none() is None

            prefs_result = await check_db.execute(
                select(NotificationPrefs).where(NotificationPrefs.user_id == user.id)
            )
            assert prefs_result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_wipe_marks_deletion_complete(self, mock_complete, mock_export, db):
        user = await _make_user(db)
        deletion = await _queue_deletion(db, user)

        await _wipe_user(user.id, deletion.id)

        async with TestSession() as check_db:
            result = await check_db.execute(
                select(AccountDeletion).where(AccountDeletion.id == deletion.id)
            )
            updated = result.scalar_one()
        assert updated.completed_at is not None

    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_wipe_writes_audit_log(self, mock_complete, mock_export, db):
        user = await _make_user(db)
        deletion = await _queue_deletion(db, user)

        await _wipe_user(user.id, deletion.id)

        async with TestSession() as check_db:
            result = await check_db.execute(
                select(AuditLog).where(
                    AuditLog.user_id == user.id,
                    AuditLog.event == AuditEvent.deletion_executed,
                )
            )
            log = result.scalar_one_or_none()
        assert log is not None
        assert "erased" in log.detail

    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_wipe_nonexistent_user_marks_complete(self, mock_complete, mock_export, db):
        """Should handle a user row that's already gone without crashing."""
        phantom_user_id = uuid.uuid4()
        AccountDeletion(
            user_id=None,  # can't FK a nonexistent user — test via direct call
            execute_after=datetime.now(UTC) - timedelta(seconds=1),
        )
        # Call the underlying helper directly with a fake deletion ID
        # This exercises the early-return branch
        fake_deletion_id = uuid.uuid4()
        # Should not raise
        await _wipe_user(phantom_user_id, fake_deletion_id)

    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_export_email_failure_does_not_abort_wipe(self, mock_complete, mock_export, db):
        """If the pre-deletion export email fails, the wipe should still proceed."""
        mock_export.side_effect = Exception("SMTP down")
        user = await _make_user(db)
        deletion = await _queue_deletion(db, user)

        # Should not raise despite email failure
        await _wipe_user(user.id, deletion.id)

        async with TestSession() as check_db:
            result = await check_db.execute(select(User).where(User.id == user.id))
            wiped = result.scalar_one()
        assert wiped.is_active is False


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULER / WORKER ORCHESTRATION
# ════════════════════════════════════════════════════════════════════════════
class TestRunPendingDeletions:
    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_run_pending_processes_due_deletions(self, mock_complete, mock_export, db):
        user = await _make_user(db)
        await _queue_deletion(db, user, grace_expired=True)

        await run_pending_deletions()

        async with TestSession() as check_db:
            result = await check_db.execute(select(User).where(User.id == user.id))
            wiped = result.scalar_one()
        assert wiped.is_active is False

    @pytest.mark.asyncio
    async def test_run_pending_skips_future_deletions(self, db):
        """Deletions whose execute_after is in the future must not be processed."""
        user = await _make_user(db, email="future@example.com")
        await _queue_deletion(db, user, grace_expired=False)  # 14 days away

        await run_pending_deletions()

        async with TestSession() as check_db:
            result = await check_db.execute(select(User).where(User.id == user.id))
            intact = result.scalar_one()
        assert intact.is_active is True  # untouched

    @pytest.mark.asyncio
    async def test_run_pending_skips_cancelled_deletions(self, db):
        user = await _make_user(db, email="cancelled@example.com")
        deletion = await _queue_deletion(db, user, grace_expired=True)

        # Cancel it
        deletion.cancelled_at = datetime.now(UTC)
        await db.commit()

        await run_pending_deletions()

        async with TestSession() as check_db:
            result = await check_db.execute(select(User).where(User.id == user.id))
            intact = result.scalar_one()
        assert intact.is_active is True

    @pytest.mark.asyncio
    @patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock)
    @patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock)
    async def test_run_pending_processes_multiple_users(self, mock_complete, mock_export, db):
        users = []
        for i in range(3):
            u = await _make_user(db, email=f"multi{i}@example.com")
            await _queue_deletion(db, u, grace_expired=True)
            users.append(u)

        await run_pending_deletions()

        async with TestSession() as check_db:
            for u in users:
                result = await check_db.execute(select(User).where(User.id == u.id))
                wiped = result.scalar_one()
                assert wiped.is_active is False


# ════════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ════════════════════════════════════════════════════════════════════════════
class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_record_factory(self, db):
        user = await _make_user(db)
        log = AuditLog.record(
            AuditEvent.login_success,
            user_id=user.id,
            detail="Test login",
            ip_address="127.0.0.1",
        )
        db.add(log)
        await db.commit()

        result = await db.execute(select(AuditLog).where(AuditLog.user_id == user.id))
        fetched = result.scalar_one()
        assert fetched.event == AuditEvent.login_success
        assert fetched.ip_address == "127.0.0.1"
        assert fetched.occurred_at is not None

    @pytest.mark.asyncio
    async def test_audit_survives_user_wipe(self, db):
        """After anonymisation the audit row must still exist (user_id goes NULL on hard delete,
        but since we anonymise we keep the FK intact)."""
        with (
            patch("app.gdpr.worker.send_data_export_email", new_callable=AsyncMock),
            patch("app.gdpr.worker.send_deletion_complete_email", new_callable=AsyncMock),
        ):
            user = await _make_user(db)
            deletion = await _queue_deletion(db, user, grace_expired=True)
            user_id = user.id

            await _wipe_user(user_id, deletion.id)

            async with TestSession() as check_db:
                result = await check_db.execute(
                    select(AuditLog).where(
                        AuditLog.user_id == user_id,
                        AuditLog.event == AuditEvent.deletion_executed,
                    )
                )
                assert result.scalar_one_or_none() is not None
