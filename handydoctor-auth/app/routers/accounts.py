"""
app/routers/accounts.py — Account management endpoints.

Prefix: /api/accounts

Endpoint map
────────────────────────────────────────────────────────────────────────────
FR-ACC-01  Registration & email verification
  POST   /register                 Create account, send verification email
  POST   /verify-email             Activate account with URL token
  POST   /resend-verification      Re-send verification email

FR-ACC-02  Login / logout / token refresh
  POST   /login                    Email+password → access token + HttpOnly cookie
  POST   /logout                   Revoke refresh token cookie
  POST   /token/refresh            Rotate refresh token → new pair
  GET    /oauth/google/initiate    Redirect to Google
  GET    /oauth/google/callback    Exchange code, issue tokens

FR-ACC-03  Profile management
  GET    /me                       Current user profile
  PATCH  /me                       Update name / DOB / gender / country
  POST   /me/change-email          Request email change (triggers verify flow)
  POST   /me/confirm-email-change  Confirm email change with URL token
  POST   /me/change-password       Change password (requires current pw)
  GET    /me/notification-prefs    Get notification preferences
  PATCH  /me/notification-prefs    Update notification preferences
  GET    /me/sessions              List active refresh-token sessions
  DELETE /me/sessions/{session_id} Revoke a specific session

FR-ACC-04  Deletion & password reset
  POST   /forgot-password          Send password-reset email
  POST   /reset-password           Apply new password via reset token
  POST   /me/request-deletion      Queue GDPR deletion (14-day grace)
  DELETE /me/cancel-deletion       Cancel pending deletion
  GET    /me/deletion-status       Check deletion queue status
  POST   /me/export                Export all personal data (GDPR Art. 20)
────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditEvent, AuditLog
from app.auth.dependencies import get_current_user, get_verified_user
from app.auth.jwt import (
    create_access_token,
    generate_refresh_token,
    generate_url_token,
    refresh_token_expires_at,
)
from app.auth.oauth import build_google_auth_url, exchange_google_code
from app.auth.password import hash_password, verify_password
from app.auth.rate_limit import (
    clear_failed_attempts,
    is_locked_out,
    record_failed_attempt,
    remaining_lockout_seconds,
)
from app.config import settings
from app.database import AsyncSessionLocal, get_db, get_redis
from app.email.sender import (
    send_data_export_email,
    send_deletion_confirmation_email,
    send_email_change_email,
    send_password_reset_email,
    send_security_alert_email,
    send_verification_email,
)
from app.gdpr.worker import build_export_payload
from app.models import (
    AccountDeletion,
    AuthProvider,
    EmailVerification,
    NotificationPrefs,
    PasswordResetToken,
    RefreshToken,
    User,
    VerificationPurpose,
)
from app.schemas import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    DeletionStatusOut,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    NotificationPrefsOut,
    RegisterRequest,
    RegisterResponse,
    RequestDeletionRequest,
    ResetPasswordRequest,
    TokenPair,
    UpdateNotificationPrefsRequest,
    UpdateProfileRequest,
    UserOut,
    VerifyEmailRequest,
    VerifyEmailResponse,
)


def _to_utc(dt: "datetime | None") -> "datetime | None":
    """Ensure a datetime is timezone-aware (UTC).
    SQLite strips tzinfo on read; PostgreSQL preserves it.
    This makes comparisons safe in both environments."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# ── Constants ────────────────────────────────────────────────────────────────
_REFRESH_COOKIE = "hd_refresh_token"
_COOKIE_KWARGS = {
    "key": _REFRESH_COOKIE,
    "httponly": True,
    "secure": True,  # HTTPS only in production
    "samesite": "lax",
    "path": "/api/accounts/token/refresh",
}
_PW_RESET_EXPIRE_MINUTES = 15
_EMAIL_VERIFY_EXPIRE_HOURS = settings.email_verification_expire_hours


# ── Helpers ──────────────────────────────────────────────────────────────────
def _set_refresh_cookie(response: Response, raw_token: str, expires_at: datetime) -> None:
    max_age = int((expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        **_COOKIE_KWARGS,
        value=raw_token,
        max_age=max_age,
        expires=expires_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        **{k: v for k, v in _COOKIE_KWARGS.items() if k != "key"}, key=_REFRESH_COOKIE
    )


async def _issue_token_pair(
    db: AsyncSession,
    response: Response,
    user: User,
    device_hint: str | None = None,
) -> TokenPair:
    """Create an access token + rotating refresh token and set the cookie."""
    access = create_access_token(user.id)
    raw_refresh, refresh_hash = generate_refresh_token()
    expires_at = refresh_token_expires_at()

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            device_hint=device_hint,
            expires_at=expires_at,
        )
    )
    await db.flush()
    _set_refresh_cookie(response, raw_refresh, expires_at)
    return TokenPair(access_token=access)


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


# ════════════════════════════════════════════════════════════════════════════
# FR-ACC-01  REGISTRATION & EMAIL VERIFICATION
# ════════════════════════════════════════════════════════════════════════════
@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """
    Create a new local account.  Sends a verification email — the account
    is inactive until the link is clicked.
    """
    email = body.email.lower()

    # Check uniqueness
    existing = await _get_user_by_email(db, email)
    if existing:
        # Intentionally vague to avoid user enumeration
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(
        email=email,
        full_name=body.full_name.strip(),
        password_hash=hash_password(body.password),
        auth_provider=AuthProvider.local,
        date_of_birth=datetime(
            body.date_of_birth.year,
            body.date_of_birth.month,
            body.date_of_birth.day,
            tzinfo=UTC,
        )
        if body.date_of_birth
        else None,
        gender=body.gender,
        country=body.country,
        is_email_verified=False,
        is_active=True,
    )
    db.add(user)
    await db.flush()

    # Create default notification prefs
    db.add(NotificationPrefs(user_id=user.id))

    # Email verification token
    raw_token, token_hash = generate_url_token()
    db.add(
        EmailVerification(
            user_id=user.id,
            token_hash=token_hash,
            purpose=VerificationPurpose.email_verify,
            expires_at=datetime.now(UTC) + timedelta(hours=_EMAIL_VERIFY_EXPIRE_HOURS),
        )
    )
    await db.flush()

    asyncio.create_task(send_verification_email(email, user.full_name, raw_token))

    return RegisterResponse(message="Account created. Please verify your email.", user_id=user.id)


@router.post("/verify-email", response_model=VerifyEmailResponse)
async def verify_email(
    body: VerifyEmailRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> VerifyEmailResponse:
    """Activate account with the token from the verification email."""
    from app.auth.jwt import _hash_token

    token_hash = _hash_token(body.token)

    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.token_hash == token_hash,
            EmailVerification.purpose == VerificationPurpose.email_verify,
            EmailVerification.used_at.is_(None),
        )
        .with_for_update()
    )
    ev = result.scalar_one_or_none()

    if ev is None or _to_utc(ev.expires_at) < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired verification link.")

    ev.used_at = datetime.now(UTC)
    user_result = await db.execute(select(User).where(User.id == ev.user_id))
    user = user_result.scalar_one()
    user.is_email_verified = True
    user.last_login_at = datetime.now(UTC)
    await db.flush()

    access = create_access_token(user.id)
    raw_refresh, refresh_hash = generate_refresh_token()
    expires_at = refresh_token_expires_at()
    db.add(RefreshToken(user_id=user.id, token_hash=refresh_hash, expires_at=expires_at))
    _set_refresh_cookie(response, raw_refresh, expires_at)

    return VerifyEmailResponse(
        message="Email verified. Welcome to HandyDoctor!", access_token=access
    )


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Re-send a verification email for the currently authenticated user."""
    if current_user.is_email_verified:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email is already verified.")

    # Invalidate old tokens
    await db.execute(
        delete(EmailVerification).where(
            EmailVerification.user_id == current_user.id,
            EmailVerification.purpose == VerificationPurpose.email_verify,
        )
    )
    raw_token, token_hash = generate_url_token()
    db.add(
        EmailVerification(
            user_id=current_user.id,
            token_hash=token_hash,
            purpose=VerificationPurpose.email_verify,
            expires_at=datetime.now(UTC) + timedelta(hours=_EMAIL_VERIFY_EXPIRE_HOURS),
        )
    )
    asyncio.create_task(
        send_verification_email(current_user.email, current_user.full_name, raw_token)
    )
    return MessageResponse(message="Verification email sent.")


# ════════════════════════════════════════════════════════════════════════════
# FR-ACC-02  LOGIN / LOGOUT / TOKEN REFRESH
# ════════════════════════════════════════════════════════════════════════════
@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> TokenPair:
    """Email + password login with brute-force protection."""
    email = body.email.lower()

    if await is_locked_out(redis, email):
        secs = await remaining_lockout_seconds(redis, email)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Account locked due to too many failed attempts. "
            f"Try again in {secs // 60}m {secs % 60}s.",
        )

    user = await _get_user_by_email(db, email)

    # Constant-time path — always do a verify to prevent timing attacks
    correct = (
        user is not None
        and user.auth_provider == AuthProvider.local
        and user.password_hash is not None
        and verify_password(body.password, user.password_hash)
    )

    if not correct:
        await record_failed_attempt(redis, email)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated.")

    await clear_failed_attempts(redis, email)
    user.last_login_at = datetime.now(UTC)

    db.add(
        AuditLog.record(
            AuditEvent.login_success,
            user_id=user.id,
            detail=f"Login via email/password. Device: {body.device_hint or 'unknown'}",
        )
    )

    return await _issue_token_pair(db, response, user, body.device_hint)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    response: Response,
    hd_refresh_token: Annotated[str | None, Cookie()] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MessageResponse:
    """Revoke the current refresh token and clear the cookie."""
    if hd_refresh_token:
        from app.auth.jwt import _hash_token

        token_hash = _hash_token(hd_refresh_token)
        await db.execute(delete(RefreshToken).where(RefreshToken.token_hash == token_hash))
    _clear_refresh_cookie(response)
    return MessageResponse(message="Logged out successfully.")


@router.post("/token/refresh", response_model=TokenPair)
async def refresh_tokens(
    response: Response,
    hd_refresh_token: Annotated[str | None, Cookie()] = None,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    """
    Rotate the refresh token.  Accepts the old token from the HttpOnly cookie,
    deletes it, issues a new pair.
    """
    if not hd_refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token.")

    from app.auth.jwt import _hash_token

    token_hash = _hash_token(hd_refresh_token)
    now = datetime.now(UTC)

    result = await db.execute(
        select(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.expires_at > now,
        )
        .with_for_update()
    )
    rt = result.scalar_one_or_none()

    if rt is None:
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token invalid or expired.")

    user_result = await db.execute(select(User).where(User.id == rt.user_id))
    user = user_result.scalar_one_or_none()

    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found.")

    # Delete old token (rotation)
    await db.delete(rt)
    return await _issue_token_pair(db, response, user, rt.device_hint)


# ── Google OAuth ─────────────────────────────────────────────────────────────
@router.get("/oauth/google/initiate")
async def oauth_google_initiate(
    redis: aioredis.Redis = Depends(get_redis),
) -> RedirectResponse:
    url = await build_google_auth_url(redis)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/google/callback", response_model=TokenPair)
async def oauth_google_callback(
    code: str,
    state: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> TokenPair:
    try:
        info = await exchange_google_code(redis, code, state)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    email = info.email.lower()

    # Try to find an existing user by Google sub, then by email
    result = await db.execute(select(User).where(User.google_sub == info.sub))
    user = result.scalar_one_or_none()

    if user is None:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

    if user is None:
        # New user — auto-create (email is implicitly verified by Google)
        user = User(
            email=email,
            full_name=info.name,
            auth_provider=AuthProvider.google,
            google_sub=info.sub,
            avatar_url=info.avatar_url,
            is_email_verified=True,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        db.add(NotificationPrefs(user_id=user.id))
    else:
        # Link Google sub to an existing local account
        if user.google_sub is None:
            user.google_sub = info.sub
        if info.avatar_url and not user.avatar_url:
            user.avatar_url = info.avatar_url

    user.last_login_at = datetime.now(UTC)
    return await _issue_token_pair(db, response, user)


# ════════════════════════════════════════════════════════════════════════════
# FR-ACC-03  PROFILE MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════
@router.get("/me", response_model=UserOut)
async def get_profile(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    return current_user


@router.patch("/me", response_model=UserOut)
async def update_profile(
    body: UpdateProfileRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> User:
    if body.full_name is not None:
        current_user.full_name = body.full_name
    if body.date_of_birth is not None:
        current_user.date_of_birth = datetime(
            body.date_of_birth.year,
            body.date_of_birth.month,
            body.date_of_birth.day,
            tzinfo=UTC,
        )
    if body.gender is not None:
        current_user.gender = body.gender
    if body.country is not None:
        current_user.country = body.country
    return current_user


@router.post("/me/change-email", response_model=MessageResponse)
async def change_email(
    body: ChangeEmailRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Step 1 of email-change flow.
    Verifies current password, stores pending_email, sends confirmation link.
    """
    if current_user.auth_provider != AuthProvider.local or not current_user.password_hash:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "OAuth accounts cannot change email via password."
        )

    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect current password.")

    new_email = body.new_email.lower()
    if new_email == current_user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "New email is the same as current.")

    existing = await _get_user_by_email(db, new_email)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already in use.")

    # Store new address pending confirmation
    current_user.pending_email = new_email

    # Invalidate old email-change tokens
    await db.execute(
        delete(EmailVerification).where(
            EmailVerification.user_id == current_user.id,
            EmailVerification.purpose == VerificationPurpose.email_change,
        )
    )
    raw_token, token_hash = generate_url_token()
    db.add(
        EmailVerification(
            user_id=current_user.id,
            token_hash=token_hash,
            purpose=VerificationPurpose.email_change,
            expires_at=datetime.now(UTC) + timedelta(hours=_EMAIL_VERIFY_EXPIRE_HOURS),
        )
    )
    asyncio.create_task(send_email_change_email(new_email, current_user.full_name, raw_token))
    return MessageResponse(message=f"Confirmation email sent to {new_email}.")


@router.post("/me/confirm-email-change", response_model=MessageResponse)
async def confirm_email_change(
    body: VerifyEmailRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Step 2: apply the new email address after the user clicks the link."""
    from app.auth.jwt import _hash_token

    token_hash = _hash_token(body.token)

    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.token_hash == token_hash,
            EmailVerification.user_id == current_user.id,
            EmailVerification.purpose == VerificationPurpose.email_change,
            EmailVerification.used_at.is_(None),
        )
        .with_for_update()
    )
    ev = result.scalar_one_or_none()
    if ev is None or _to_utc(ev.expires_at) < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token.")

    if not current_user.pending_email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No email change pending.")

    ev.used_at = datetime.now(UTC)
    current_user.email = current_user.pending_email
    current_user.pending_email = None

    asyncio.create_task(
        send_security_alert_email(
            current_user.email, current_user.full_name, "Email address changed"
        )
    )
    return MessageResponse(message="Email address updated successfully.")


@router.post("/me/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    if current_user.auth_provider != AuthProvider.local or not current_user.password_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth accounts do not have a password.")

    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect current password.")

    current_user.password_hash = hash_password(body.new_password)

    # Revoke all refresh tokens (force re-login everywhere)
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == current_user.id))

    asyncio.create_task(
        send_security_alert_email(current_user.email, current_user.full_name, "Password changed")
    )
    return MessageResponse(message="Password updated. All sessions have been signed out.")


# ── Notification preferences ─────────────────────────────────────────────────
@router.get("/me/notification-prefs", response_model=NotificationPrefsOut)
async def get_notification_prefs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> NotificationPrefs:
    result = await db.execute(
        select(NotificationPrefs).where(NotificationPrefs.user_id == current_user.id)
    )
    prefs = result.scalar_one_or_none()
    if prefs is None:
        prefs = NotificationPrefs(user_id=current_user.id)
        db.add(prefs)
        await db.flush()
    return prefs


@router.patch("/me/notification-prefs", response_model=NotificationPrefsOut)
async def update_notification_prefs(
    body: UpdateNotificationPrefsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> NotificationPrefs:
    result = await db.execute(
        select(NotificationPrefs).where(NotificationPrefs.user_id == current_user.id)
    )
    prefs = result.scalar_one_or_none()
    if prefs is None:
        prefs = NotificationPrefs(user_id=current_user.id)
        db.add(prefs)
        await db.flush()

    if body.session_summaries is not None:
        prefs.session_summaries = body.session_summaries
    if body.followup_reminders is not None:
        prefs.followup_reminders = body.followup_reminders
    if body.product_updates is not None:
        prefs.product_updates = body.product_updates
    if body.security_alerts is not None:
        prefs.security_alerts = body.security_alerts
    return prefs


# ── Active sessions ──────────────────────────────────────────────────────────
@router.get("/me/sessions")
async def list_sessions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(
        select(RefreshToken)
        .where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.expires_at > datetime.now(UTC),
        )
        .order_by(RefreshToken.created_at.desc())
    )
    sessions = result.scalars().all()
    return [
        {
            "id": str(s.id),
            "device_hint": s.device_hint,
            "created_at": s.created_at.isoformat(),
            "expires_at": s.expires_at.isoformat(),
        }
        for s in sessions
    ]


@router.delete("/me/sessions/{session_id}", response_model=MessageResponse)
async def revoke_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.id == session_id,
            RefreshToken.user_id == current_user.id,
        )
    )
    rt = result.scalar_one_or_none()
    if rt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    await db.delete(rt)
    return MessageResponse(message="Session revoked.")


# ════════════════════════════════════════════════════════════════════════════
# FR-ACC-04  PASSWORD RESET & ACCOUNT DELETION
# ════════════════════════════════════════════════════════════════════════════
@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Always returns 200 to prevent email enumeration."""
    user = await _get_user_by_email(db, body.email.lower())
    if user and user.auth_provider == AuthProvider.local:
        # Invalidate any existing reset tokens
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
        raw_token, token_hash = generate_url_token()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(minutes=_PW_RESET_EXPIRE_MINUTES),
            )
        )
        asyncio.create_task(send_password_reset_email(user.email, user.full_name, raw_token))
    return MessageResponse(
        message="If an account with that email exists, a reset link has been sent."
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    from app.auth.jwt import _hash_token

    token_hash = _hash_token(body.token)

    result = await db.execute(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
        )
        .with_for_update()
    )
    prt = result.scalar_one_or_none()

    if prt is None or _to_utc(prt.expires_at) < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token.")

    user_result = await db.execute(select(User).where(User.id == prt.user_id))
    user = user_result.scalar_one()
    user.password_hash = hash_password(body.new_password)

    prt.used_at = datetime.now(UTC)

    # Revoke all sessions after password reset
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))

    asyncio.create_task(
        send_security_alert_email(user.email, user.full_name, "Password reset successfully")
    )
    return MessageResponse(message="Password reset successfully. Please sign in again.")


# ── GDPR Account Deletion ─────────────────────────────────────────────────────
@router.post("/me/request-deletion", response_model=DeletionStatusOut, status_code=201)
async def request_deletion(
    body: RequestDeletionRequest,
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> AccountDeletion:
    """Queue account for GDPR deletion with 14-day grace period."""
    if current_user.auth_provider == AuthProvider.local:
        if not current_user.password_hash or not verify_password(
            body.current_password, current_user.password_hash
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password.")

    # Check if already queued
    result = await db.execute(
        select(AccountDeletion).where(
            AccountDeletion.user_id == current_user.id,
            AccountDeletion.cancelled_at.is_(None),
            AccountDeletion.completed_at.is_(None),
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Deletion already requested.")

    now = datetime.now(UTC)
    execute_after = now + timedelta(days=settings.account_deletion_grace_days)

    deletion = AccountDeletion(
        user_id=current_user.id,
        execute_after=execute_after,
    )
    db.add(deletion)
    await db.flush()

    db.add(
        AuditLog.record(
            AuditEvent.deletion_requested,
            user_id=current_user.id,
            detail=f"Deletion scheduled for {execute_after.strftime('%Y-%m-%d')} UTC.",
        )
    )

    asyncio.create_task(
        send_deletion_confirmation_email(
            current_user.email,
            current_user.full_name,
            execute_after.strftime("%d %B %Y"),
        )
    )
    return deletion


@router.delete("/me/cancel-deletion", response_model=MessageResponse)
async def cancel_deletion(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await db.execute(
        select(AccountDeletion)
        .where(
            AccountDeletion.user_id == current_user.id,
            AccountDeletion.cancelled_at.is_(None),
            AccountDeletion.completed_at.is_(None),
        )
        .with_for_update()
    )
    deletion = result.scalar_one_or_none()
    if deletion is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No pending deletion request found.")

    deletion.cancelled_at = datetime.now(UTC)
    db.add(AuditLog.record(AuditEvent.deletion_cancelled, user_id=current_user.id))
    return MessageResponse(message="Account deletion cancelled. Your account is safe.")


@router.get("/me/deletion-status", response_model=DeletionStatusOut)
async def deletion_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> AccountDeletion:
    result = await db.execute(
        select(AccountDeletion).where(
            AccountDeletion.user_id == current_user.id,
            AccountDeletion.cancelled_at.is_(None),
            AccountDeletion.completed_at.is_(None),
        )
    )
    deletion = result.scalar_one_or_none()
    if deletion is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active deletion request.")
    return deletion


# ── GDPR data export (Article 20) ────────────────────────────────────────────
@router.post("/me/export", response_model=MessageResponse)
async def export_my_data(
    current_user: Annotated[User, Depends(get_verified_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """
    Build a complete JSON export of all personal data held about the user
    and email it to their verified address.  Fulfilment is asynchronous —
    the endpoint returns immediately and the email arrives within seconds.
    """
    import json

    export = await build_export_payload(db, current_user)
    export_json = json.dumps(export, indent=2, default=str)

    db.add(
        AuditLog.record(
            AuditEvent.data_export_requested,
            user_id=current_user.id,
            detail="Data export requested via /me/export",
        )
    )

    asyncio.create_task(
        send_data_export_email(
            to=current_user.email,
            full_name=current_user.full_name,
            json_payload=export_json,
            is_deletion_export=False,
        )
    )

    # Fire-and-forget audit update once email is confirmed sent
    async def _mark_sent() -> None:
        async with AsyncSessionLocal() as audit_db:
            audit_db.add(
                AuditLog.record(
                    AuditEvent.data_export_sent,
                    user_id=current_user.id,
                    detail=f"Export emailed to {current_user.email}",
                )
            )
            await audit_db.commit()

    asyncio.create_task(_mark_sent())

    return MessageResponse(
        message=f"Your data export has been sent to {current_user.email}. "
        "Check your inbox within a few minutes."
    )
