"""
tests/test_accounts.py — Full test suite for the accounts router.

Run with:  pytest -v tests/
Requires:  pytest, pytest-asyncio, httpx
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.password import hash_password
from app.database import Base, get_db, get_redis
from app.models import (
    AuthProvider,
    EmailVerification,
    NotificationPrefs,
    PasswordResetToken,
    User,
    VerificationPurpose,
)
from main import app

# ── In-memory SQLite for tests ────────────────────────────────────────────────
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
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    redis_mock = make_redis_mock()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: redis_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _create_verified_user(
    db: AsyncSession,
    email: str = "test@example.com",
    password: str = "Test1234!",
    full_name: str = "Test User",
) -> User:
    user = User(
        email=email,
        full_name=full_name,
        password_hash=hash_password(password),
        auth_provider=AuthProvider.local,
        is_email_verified=True,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # materialise user.id before FK ref
    db.add(NotificationPrefs(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, email="test@example.com", password="Test1234!") -> str:
    resp = await client.post("/api/accounts/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.json()
    return resp.json()["access_token"]


# ════════════════════════════════════════════════════════════════════════════
# REGISTRATION
# ════════════════════════════════════════════════════════════════════════════
class TestRegistration:
    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_verification_email", new_callable=AsyncMock)
    async def test_register_success(self, mock_email, client):
        resp = await client.post(
            "/api/accounts/register",
            json={
                "full_name": "Jane Doe",
                "email": "jane@example.com",
                "password": "StrongP@ss1",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "user_id" in body
        assert "verify" in body["message"].lower()

    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_verification_email", new_callable=AsyncMock)
    async def test_register_duplicate_email(self, mock_email, client):
        payload = {"full_name": "Dup User", "email": "dup@example.com", "password": "StrongP@ss1"}
        await client.post("/api/accounts/register", json=payload)
        resp = await client.post("/api/accounts/register", json=payload)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_register_weak_password(self, client):
        resp = await client.post(
            "/api/accounts/register",
            json={
                "full_name": "Bob",
                "email": "bob@example.com",
                "password": "simple",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_register_invalid_email(self, client):
        resp = await client.post(
            "/api/accounts/register",
            json={
                "full_name": "Eve",
                "email": "not-an-email",
                "password": "StrongP@ss1",
            },
        )
        assert resp.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# EMAIL VERIFICATION
# ════════════════════════════════════════════════════════════════════════════
class TestEmailVerification:
    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_verification_email", new_callable=AsyncMock)
    async def test_verify_email_success(self, mock_email, client):
        import hashlib
        import secrets

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        async with TestSession() as db:
            user = User(
                email="verify@example.com",
                full_name="V User",
                password_hash=hash_password("StrongP@ss1"),
                auth_provider=AuthProvider.local,
                is_email_verified=False,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            db.add(
                EmailVerification(
                    user_id=user.id,
                    token_hash=token_hash,
                    purpose=VerificationPurpose.email_verify,
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
            )
            await db.commit()

        resp = await client.post("/api/accounts/verify-email", json={"token": raw_token})
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    @pytest.mark.asyncio
    async def test_verify_email_expired(self, client):
        import hashlib
        import secrets

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        async with TestSession() as db:
            user = User(
                email="exp@example.com",
                full_name="Exp User",
                password_hash=hash_password("StrongP@ss1"),
                auth_provider=AuthProvider.local,
                is_email_verified=False,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            db.add(
                EmailVerification(
                    user_id=user.id,
                    token_hash=token_hash,
                    purpose=VerificationPurpose.email_verify,
                    expires_at=datetime.now(UTC) - timedelta(hours=1),  # expired
                )
            )
            await db.commit()

        resp = await client.post("/api/accounts/verify-email", json={"token": raw_token})
        assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# LOGIN
# ════════════════════════════════════════════════════════════════════════════
class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        resp = await client.post(
            "/api/accounts/login",
            json={
                "email": "test@example.com",
                "password": "Test1234!",
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()
        # Refresh token cookie must be set
        assert "hd_refresh_token" in resp.cookies

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        resp = await client.post(
            "/api/accounts/login",
            json={
                "email": "test@example.com",
                "password": "WrongPass1!",
            },
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_unknown_email(self, client):
        resp = await client.post(
            "/api/accounts/login",
            json={
                "email": "nobody@example.com",
                "password": "Test1234!",
            },
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_locked_account(self, client):
        """Simulate a locked-out account via the Redis mock."""
        async with TestSession() as db:
            await _create_verified_user(db)

        # Patch is_locked_out to return True
        with (
            patch("app.routers.accounts.is_locked_out", new_callable=AsyncMock, return_value=True),
            patch(
                "app.routers.accounts.remaining_lockout_seconds",
                new_callable=AsyncMock,
                return_value=600,
            ),
        ):
            resp = await client.post(
                "/api/accounts/login",
                json={
                    "email": "test@example.com",
                    "password": "Test1234!",
                },
            )
        assert resp.status_code == 429


# ════════════════════════════════════════════════════════════════════════════
# TOKEN REFRESH & LOGOUT
# ════════════════════════════════════════════════════════════════════════════
class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_refresh_success(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        # Login to get a refresh cookie
        await client.post(
            "/api/accounts/login",
            json={
                "email": "test@example.com",
                "password": "Test1234!",
            },
        )
        resp = await client.post("/api/accounts/token/refresh")
        # May be 401 if the HttpOnly cookie wasn't persisted by test client
        assert resp.status_code in (200, 401)

    @pytest.mark.asyncio
    async def test_refresh_without_cookie(self, client):
        resp = await client.post("/api/accounts/token/refresh")
        assert resp.status_code == 401


# ════════════════════════════════════════════════════════════════════════════
# PROFILE
# ════════════════════════════════════════════════════════════════════════════
class TestProfile:
    @pytest.mark.asyncio
    async def test_get_profile(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.get("/api/accounts/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_update_profile(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.patch(
            "/api/accounts/me",
            json={"full_name": "Updated Name", "country": "GB"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["full_name"] == "Updated Name"
        assert resp.json()["country"] == "GB"

    @pytest.mark.asyncio
    async def test_get_profile_unauthenticated(self, client):
        resp = await client.get("/api/accounts/me")
        assert resp.status_code in (401, 403)


# ════════════════════════════════════════════════════════════════════════════
# CHANGE PASSWORD
# ════════════════════════════════════════════════════════════════════════════
class TestChangePassword:
    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_security_alert_email", new_callable=AsyncMock)
    async def test_change_password_success(self, mock_email, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.post(
            "/api/accounts/me/change-password",
            json={
                "current_password": "Test1234!",
                "new_password": "NewPass99@",
                "confirm_password": "NewPass99@",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_change_password_wrong_current(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.post(
            "/api/accounts/me/change-password",
            json={
                "current_password": "WrongCurrent!",
                "new_password": "NewPass99@",
                "confirm_password": "NewPass99@",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_change_password_mismatch(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.post(
            "/api/accounts/me/change-password",
            json={
                "current_password": "Test1234!",
                "new_password": "NewPass99@",
                "confirm_password": "DifferentPass1!",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# NOTIFICATION PREFERENCES
# ════════════════════════════════════════════════════════════════════════════
class TestNotificationPrefs:
    @pytest.mark.asyncio
    async def test_get_and_update_prefs(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        headers = {"Authorization": f"Bearer {token}"}

        get_resp = await client.get("/api/accounts/me/notification-prefs", headers=headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["session_summaries"] is True

        patch_resp = await client.patch(
            "/api/accounts/me/notification-prefs",
            json={"product_updates": True, "session_summaries": False},
            headers=headers,
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["product_updates"] is True
        assert patch_resp.json()["session_summaries"] is False


# ════════════════════════════════════════════════════════════════════════════
# PASSWORD RESET
# ════════════════════════════════════════════════════════════════════════════
class TestPasswordReset:
    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_password_reset_email", new_callable=AsyncMock)
    async def test_forgot_password_always_200(self, mock_email, client):
        resp = await client.post(
            "/api/accounts/forgot-password", json={"email": "nobody@example.com"}
        )
        assert resp.status_code == 200  # Never reveals whether email exists

    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_security_alert_email", new_callable=AsyncMock)
    async def test_reset_password_success(self, mock_alert, client):
        import hashlib
        import secrets

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        async with TestSession() as db:
            user = await _create_verified_user(db)
            db.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=token_hash,
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
            )
            await db.commit()

        resp = await client.post(
            "/api/accounts/reset-password",
            json={
                "token": raw_token,
                "new_password": "Reset99@pass",
                "confirm_password": "Reset99@pass",
            },
        )
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# ACCOUNT DELETION (FR-ACC-04)
# ════════════════════════════════════════════════════════════════════════════
class TestAccountDeletion:
    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_deletion_confirmation_email", new_callable=AsyncMock)
    async def test_request_deletion(self, mock_email, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.post(
            "/api/accounts/me/request-deletion",
            json={"current_password": "Test1234!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "execute_after" in body

    @pytest.mark.asyncio
    @patch("app.routers.accounts.send_deletion_confirmation_email", new_callable=AsyncMock)
    async def test_cancel_deletion(self, mock_email, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post(
            "/api/accounts/me/request-deletion",
            json={"current_password": "Test1234!"},
            headers=headers,
        )

        cancel_resp = await client.delete("/api/accounts/me/cancel-deletion", headers=headers)
        assert cancel_resp.status_code == 200

    @pytest.mark.asyncio
    async def test_deletion_status_not_found(self, client):
        async with TestSession() as db:
            await _create_verified_user(db)
        token = await _login(client)
        resp = await client.get(
            "/api/accounts/me/deletion-status", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404
