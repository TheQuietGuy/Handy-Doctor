"""tests/test_admin.py -- tests for the admin router (/api/admin/*)."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit import AuditEvent, AuditLog
from app.auth.password import hash_password
from app.chat.models import ChatMessage, ChatSession, MessageRole, Severity
from app.database import Base, get_db, get_redis
from app.models import AccountDeletion, AuthProvider, NotificationPrefs, User
from app.notifications.models import NotificationLog, NotificationStatus, NotificationType
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


def _make_redis_mock():
    m = AsyncMock()
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[0, 0, None, True, []])
    pipe.zremrangebyscore = MagicMock(return_value=None)
    pipe.zcard = MagicMock(return_value=None)
    pipe.zadd = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.zrange = MagicMock(return_value=None)
    m.pipeline = MagicMock(return_value=pipe)
    m.exists = AsyncMock(return_value=0)
    m.incr = AsyncMock(return_value=1)
    m.expire = AsyncMock(return_value=True)
    m.set = AsyncMock(return_value=True)
    m.get = AsyncMock(return_value=None)
    m.delete = AsyncMock(return_value=1)
    m.ttl = AsyncMock(return_value=-2)
    return m


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: _make_redis_mock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db():
    async with TestSession() as session:
        yield session
        await session.rollback()


async def _make_user(
    db: AsyncSession,
    email: str = "user@example.com",
    full_name: str = "Regular User",
    password: str = "Test1234!",
    is_superuser: bool = False,
    is_active: bool = True,
) -> User:
    user = User(
        email=email,
        full_name=full_name,
        password_hash=hash_password(password),
        auth_provider=AuthProvider.local,
        is_email_verified=True,
        is_active=is_active,
        is_superuser=is_superuser,
    )
    db.add(user)
    await db.flush()
    db.add(NotificationPrefs(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, email: str, password: str = "Test1234!") -> str:
    resp = await client.post("/api/accounts/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_admin(db, client, email="admin@example.com"):
    admin = await _make_user(db, email=email, is_superuser=True)
    token = await _login(client, email)
    return admin, token


# ============================================================
# 1. AUTHORISATION
# ============================================================
class TestAdminAuthorisation:
    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, client):
        resp = await client.get("/api/admin/stats")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self, client):
        async with TestSession() as db:
            await _make_user(db, email="plain@example.com")
        token = await _login(client, "plain@example.com")
        resp = await client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_allowed(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        resp = await client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


# ============================================================
# 2. SYSTEM STATS
# ============================================================
class TestSystemStats:
    @pytest.mark.asyncio
    async def test_counts_users(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            await _make_user(db, email="u1@example.com")
            await _make_user(db, email="u2@example.com", is_active=False)
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total_users"] == 3
        assert body["active_users"] == 2

    @pytest.mark.asyncio
    async def test_includes_session_counts(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            user = await _make_user(db, email="chatter@example.com")
            session = ChatSession(user_id=user.id, title="Test", message_count=2)
            db.add(session)
            await db.flush()
            db.add(ChatMessage(session_id=session.id, role=MessageRole.user, content="hi"))
            db.add(ChatMessage(session_id=session.id, role=MessageRole.assistant,
                               content="hello", severity=Severity.green))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total_chat_sessions"] == 1
        assert body["total_chat_messages"] == 2
        assert body["severity_breakdown_last_7d"]["green"] == 1

    @pytest.mark.asyncio
    async def test_notification_breakdown_keys_present(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})).json()
        for key in ("sent", "failed", "skipped"):
            assert key in body["notification_logs_last_24h"]


# ============================================================
# 3. USER LISTING & DETAIL
# ============================================================
class TestUserManagementRead:
    @pytest.mark.asyncio
    async def test_list_all_users(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            await _make_user(db, email="alice@example.com")
            await _make_user(db, email="bob@example.com")
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 3

    @pytest.mark.asyncio
    async def test_search_by_email(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            await _make_user(db, email="findme@example.com")
            await _make_user(db, email="other@example.com")
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/users?search=findme",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 1
        assert body["users"][0]["email"] == "findme@example.com"

    @pytest.mark.asyncio
    async def test_filter_by_active_false(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            await _make_user(db, email="inactive@example.com", is_active=False)
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/users?is_active=false",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 1
        assert body["users"][0]["email"] == "inactive@example.com"

    @pytest.mark.asyncio
    async def test_pagination(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            for i in range(5):
                await _make_user(db, email=f"page{i}@example.com")
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/users?offset=0&limit=2",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert len(body["users"]) == 2
        assert body["total"] == 6

    @pytest.mark.asyncio
    async def test_get_user_detail(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="detail@example.com")
        token = await _login(client, "admin@example.com")
        resp = await client.get(f"/api/admin/users/{target.id}",
                                 headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "detail@example.com"

    @pytest.mark.asyncio
    async def test_get_user_detail_404(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        resp = await client.get(f"/api/admin/users/{uuid.uuid4()}",
                                 headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_shows_pending_deletion(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="deleting@example.com")
            db.add(AccountDeletion(
                user_id=target.id,
                execute_after=datetime.now(timezone.utc) + timedelta(days=14),
            ))
            await db.commit()
        token = await _login(client, "admin@example.com")
        resp = await client.get(f"/api/admin/users/{target.id}",
                                 headers={"Authorization": f"Bearer {token}"})
        assert resp.json()["has_pending_deletion"] is True


# ============================================================
# 4. USER MUTATIONS
# ============================================================
class TestUserMutations:
    @pytest.mark.asyncio
    async def test_deactivate_user(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="target@example.com")
        token = await _login(client, "admin@example.com")
        resp = await client.patch(f"/api/admin/users/{target.id}/active",
                                   json={"is_active": False, "reason": "Test"},
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        async with TestSession() as db:
            user = (await db.execute(select(User).where(User.id == target.id))).scalar_one()
            assert user.is_active is False

    @pytest.mark.asyncio
    async def test_deactivate_writes_audit_log(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="audited@example.com")
        token = await _login(client, "admin@example.com")
        await client.patch(f"/api/admin/users/{target.id}/active",
                           json={"is_active": False, "reason": "TestReason"},
                           headers={"Authorization": f"Bearer {token}"})
        async with TestSession() as db:
            log = (await db.execute(
                select(AuditLog).where(
                    AuditLog.user_id == target.id,
                    AuditLog.event == AuditEvent.admin_user_deactivated,
                )
            )).scalar_one_or_none()
            assert log is not None
            assert "TestReason" in log.detail

    @pytest.mark.asyncio
    async def test_admin_cannot_deactivate_self(self, client):
        async with TestSession() as db:
            admin = await _make_user(db, email="admin@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        resp = await client.patch(f"/api/admin/users/{admin.id}/active",
                                   json={"is_active": False},
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_grant_superuser(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="promote@example.com")
        token = await _login(client, "admin@example.com")
        resp = await client.patch(f"/api/admin/users/{target.id}/superuser",
                                   json={"is_superuser": True},
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "granted" in resp.json()["message"]
        async with TestSession() as db:
            user = (await db.execute(select(User).where(User.id == target.id))).scalar_one()
            assert user.is_superuser is True

    @pytest.mark.asyncio
    async def test_revoke_superuser_writes_audit_log(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="demote@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        await client.patch(f"/api/admin/users/{target.id}/superuser",
                           json={"is_superuser": False},
                           headers={"Authorization": f"Bearer {token}"})
        async with TestSession() as db:
            log = (await db.execute(
                select(AuditLog).where(
                    AuditLog.user_id == target.id,
                    AuditLog.event == AuditEvent.admin_superuser_revoked,
                )
            )).scalar_one_or_none()
            assert log is not None

    @pytest.mark.asyncio
    async def test_admin_cannot_revoke_own_superuser(self, client):
        async with TestSession() as db:
            admin = await _make_user(db, email="admin@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        resp = await client.patch(f"/api/admin/users/{admin.id}/superuser",
                                   json={"is_superuser": False},
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_mutate_nonexistent_user_404(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
        token = await _login(client, "admin@example.com")
        resp = await client.patch(f"/api/admin/users/{uuid.uuid4()}/active",
                                   json={"is_active": False},
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_non_admin_cannot_mutate(self, client):
        async with TestSession() as db:
            await _make_user(db, email="plain@example.com")
            target = await _make_user(db, email="victim@example.com")
        token = await _login(client, "plain@example.com")
        resp = await client.patch(f"/api/admin/users/{target.id}/active",
                                   json={"is_active": False},
                                   headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403


# ============================================================
# 5. AUDIT LOGS
# ============================================================
class TestAuditLogEndpoint:
    @pytest.mark.asyncio
    async def test_list_audit_logs(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            user = await _make_user(db, email="logged@example.com")
            db.add(AuditLog.record(AuditEvent.login_success, user_id=user.id))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/audit-logs",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] >= 1

    @pytest.mark.asyncio
    async def test_filter_by_event(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            user = await _make_user(db, email="filterlog@example.com")
            db.add(AuditLog.record(AuditEvent.login_success, user_id=user.id))
            db.add(AuditLog.record(AuditEvent.logout, user_id=user.id))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/audit-logs?event=logout",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert all(log["event"] == "logout" for log in body["logs"])

    @pytest.mark.asyncio
    async def test_includes_user_email(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            user = await _make_user(db, email="emailcheck@example.com")
            db.add(AuditLog.record(AuditEvent.login_success, user_id=user.id))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/audit-logs",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert "emailcheck@example.com" in [log["user_email"] for log in body["logs"]]

    @pytest.mark.asyncio
    async def test_filter_by_user_id(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            u1 = await _make_user(db, email="u1@example.com")
            u2 = await _make_user(db, email="u2@example.com")
            db.add(AuditLog.record(AuditEvent.login_success, user_id=u1.id))
            db.add(AuditLog.record(AuditEvent.login_success, user_id=u2.id))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get(f"/api/admin/audit-logs?user_id={u1.id}",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert all(log["user_id"] == str(u1.id) for log in body["logs"])


# ============================================================
# 6. NOTIFICATION LOGS
# ============================================================
class TestNotificationLogEndpoint:
    @pytest.mark.asyncio
    async def test_list_all(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            user = await _make_user(db, email="notiflog@example.com")
            db.add(NotificationLog(user_id=user.id,
                                   notification_type=NotificationType.session_summary,
                                   status=NotificationStatus.sent))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/notification-logs",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 1

    @pytest.mark.asyncio
    async def test_filter_by_status_failed(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            user = await _make_user(db, email="statuslog@example.com")
            db.add(NotificationLog(user_id=user.id,
                                   notification_type=NotificationType.session_summary,
                                   status=NotificationStatus.failed,
                                   error_detail="SMTP timeout"))
            db.add(NotificationLog(user_id=user.id,
                                   notification_type=NotificationType.followup_reminder,
                                   status=NotificationStatus.sent))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/notification-logs?status=failed",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 1
        assert body["logs"][0]["error_detail"] == "SMTP timeout"


# ============================================================
# 7. DELETION QUEUE
# ============================================================
class TestDeletionQueueEndpoint:
    @pytest.mark.asyncio
    async def test_list_pending(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="pendingdel@example.com")
            db.add(AccountDeletion(
                user_id=target.id,
                execute_after=datetime.now(timezone.utc) + timedelta(days=14),
            ))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/deletions",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 1
        assert body["deletions"][0]["user_email"] == "pendingdel@example.com"

    @pytest.mark.asyncio
    async def test_cancelled_excluded_by_default(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="cancelled@example.com")
            db.add(AccountDeletion(
                user_id=target.id,
                execute_after=datetime.now(timezone.utc) + timedelta(days=14),
                cancelled_at=datetime.now(timezone.utc),
            ))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/deletions",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 0

    @pytest.mark.asyncio
    async def test_include_cancelled_flag(self, client):
        async with TestSession() as db:
            await _make_user(db, email="admin@example.com", is_superuser=True)
            target = await _make_user(db, email="cancelled2@example.com")
            db.add(AccountDeletion(
                user_id=target.id,
                execute_after=datetime.now(timezone.utc) + timedelta(days=14),
                cancelled_at=datetime.now(timezone.utc),
            ))
            await db.commit()
        token = await _login(client, "admin@example.com")
        body = (await client.get("/api/admin/deletions?include_cancelled=true",
                                  headers={"Authorization": f"Bearer {token}"})).json()
        assert body["total"] == 1
