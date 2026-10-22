"""
app/schemas.py — Pydantic v2 request and response schemas.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


# ── Shared config ────────────────────────────────────────────────────────────
class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════════════
# REGISTRATION  (FR-ACC-01)
# ═══════════════════════════════════════════════════════════════════════════
class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    date_of_birth: date | None = None
    gender: str | None = None
    country: str | None = Field(default=None, max_length=100)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        errors: list[str] = []
        if not any(c.isupper() for c in v):
            errors.append("one uppercase letter")
        if not any(c.isdigit() for c in v):
            errors.append("one digit")
        if not any(c in "!@#$%^&*()-_=+[]{}|;:,.<>?" for c in v):
            errors.append("one special character")
        if errors:
            raise ValueError(f"Password must contain: {', '.join(errors)}.")
        return v

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class RegisterResponse(BaseModel):
    message: str
    user_id: uuid.UUID


# ═══════════════════════════════════════════════════════════════════════════
# EMAIL VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════
class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=32)


class VerifyEmailResponse(BaseModel):
    message: str
    access_token: str
    token_type: str = "bearer"


# ═══════════════════════════════════════════════════════════════════════════
# LOGIN  (FR-ACC-02)
# ═══════════════════════════════════════════════════════════════════════════
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    device_hint: str | None = Field(default=None, max_length=200)


class TokenPair(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # refresh_token is set in an HttpOnly cookie — not returned in JSON body


class RefreshRequest(BaseModel):
    # The raw refresh token read from the HttpOnly cookie in the router
    refresh_token: str


# ═══════════════════════════════════════════════════════════════════════════
# USER PROFILE
# ═══════════════════════════════════════════════════════════════════════════
class UserOut(_ORM):
    id: uuid.UUID
    email: str
    full_name: str
    date_of_birth: date | None
    gender: str | None
    country: str | None
    avatar_url: str | None
    is_email_verified: bool
    auth_provider: str
    created_at: datetime
    last_login_at: datetime | None


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE EDIT  (FR-ACC-03)
# ═══════════════════════════════════════════════════════════════════════════
class UpdateProfileRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    date_of_birth: date | None = None
    gender: str | None = None
    country: str | None = Field(default=None, max_length=100)

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, v: str | None) -> str | None:
        return v.strip() if v else v


class ChangeEmailRequest(BaseModel):
    new_email: EmailStr
    current_password: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=72)
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self) -> "ChangePasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("new_password and confirm_password must match.")
        return self

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        errors: list[str] = []
        if not any(c.isupper() for c in v):
            errors.append("one uppercase letter")
        if not any(c.isdigit() for c in v):
            errors.append("one digit")
        if not any(c in "!@#$%^&*()-_=+[]{}|;:,.<>?" for c in v):
            errors.append("one special character")
        if errors:
            raise ValueError(f"Password must contain: {', '.join(errors)}.")
        return v


# ═══════════════════════════════════════════════════════════════════════════
# NOTIFICATION PREFERENCES
# ═══════════════════════════════════════════════════════════════════════════
class NotificationPrefsOut(_ORM):
    session_summaries: bool
    followup_reminders: bool
    product_updates: bool
    security_alerts: bool


class UpdateNotificationPrefsRequest(BaseModel):
    session_summaries: bool | None = None
    followup_reminders: bool | None = None
    product_updates: bool | None = None
    security_alerts: bool | None = None


# ═══════════════════════════════════════════════════════════════════════════
# PASSWORD RESET
# ═══════════════════════════════════════════════════════════════════════════
class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=32)
    new_password: str = Field(min_length=8, max_length=72)
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self) -> "ResetPasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords must match.")
        return self


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT DELETION  (FR-ACC-04)
# ═══════════════════════════════════════════════════════════════════════════
class RequestDeletionRequest(BaseModel):
    current_password: str = Field(min_length=1)


class DeletionStatusOut(_ORM):
    requested_at: datetime
    execute_after: datetime
    cancelled_at: datetime | None


# ═══════════════════════════════════════════════════════════════════════════
# GOOGLE OAUTH
# ═══════════════════════════════════════════════════════════════════════════
class OAuthCallbackQuery(BaseModel):
    code: str
    state: str
    device_hint: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# GENERIC
# ═══════════════════════════════════════════════════════════════════════════
class MessageResponse(BaseModel):
    message: str
