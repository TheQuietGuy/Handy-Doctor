"""
app/routers/admin_schemas.py — Request/response schemas for the admin API.

Kept in a separate module from app/schemas.py because these are
admin-surface-only types that should never leak into the public API docs
grouping, and because admin responses intentionally expose fields
(is_active, is_superuser, raw counts) that the regular UserOut schema
deliberately omits from end-user responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Dashboard ──────────────────────────────────────────────────────────────────
class SystemStatsOut(BaseModel):
    total_users: int
    active_users: int
    verified_users: int
    users_pending_deletion: int
    total_chat_sessions: int
    total_chat_messages: int
    sessions_last_24h: int
    messages_last_24h: int
    notification_logs_last_24h: dict[str, int]   # {"sent": n, "failed": n, "skipped": n}
    severity_breakdown_last_7d: dict[str, int]   # {"green": n, "amber": n, "red": n}


# ── User management ───────────────────────────────────────────────────────────
class AdminUserOut(_ORM):
    id:                 uuid.UUID
    email:              str
    full_name:          str
    auth_provider:      str
    is_active:          bool
    is_email_verified:  bool
    is_superuser:       bool
    created_at:         datetime
    last_login_at:      datetime | None
    chat_session_count: int = 0
    has_pending_deletion: bool = False


class AdminUserListOut(BaseModel):
    users: list[AdminUserOut]
    total: int
    offset: int
    limit: int


class SetUserActiveRequest(BaseModel):
    is_active: bool
    reason: str | None = Field(default=None, max_length=500)


class SetSuperuserRequest(BaseModel):
    is_superuser: bool


# ── Audit log ──────────────────────────────────────────────────────────────────
class AdminAuditLogOut(_ORM):
    id:           uuid.UUID
    user_id:      uuid.UUID | None
    user_email:   str | None = None   # populated via join when available
    event:        str
    detail:       str | None
    ip_address:   str | None
    occurred_at:  datetime


class AuditLogListOut(BaseModel):
    logs:   list[AdminAuditLogOut]
    total:  int
    offset: int
    limit:  int


# ── Notification log ─────────────────────────────────────────────────────────
class AdminNotificationLogOut(_ORM):
    id:                 uuid.UUID
    user_id:            uuid.UUID | None
    session_id:         uuid.UUID | None
    notification_type:  str
    status:             str
    error_detail:       str | None
    sent_at:            datetime


class NotificationLogListOut(BaseModel):
    logs:   list[AdminNotificationLogOut]
    total:  int
    offset: int
    limit:  int


# ── GDPR deletion queue ───────────────────────────────────────────────────────
class AdminDeletionQueueOut(_ORM):
    id:             uuid.UUID
    user_id:        uuid.UUID
    user_email:      str | None = None
    requested_at:   datetime
    execute_after:  datetime
    cancelled_at:   datetime | None
    completed_at:   datetime | None


class DeletionQueueListOut(BaseModel):
    deletions: list[AdminDeletionQueueOut]
    total:     int


# ── Generic ───────────────────────────────────────────────────────────────────
class MessageResponse(BaseModel):
    message: str
