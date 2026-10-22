"""
app/routers/admin.py — Administrative endpoints.

Prefix: /api/admin
All routes require get_admin_user (is_superuser = True). A non-admin
authenticated user gets 403; an unauthenticated request gets 401.

Endpoint map
────────────────────────────────────────────────────────────────────────────
GET    /stats                       System-wide dashboard statistics
GET    /users                       List/search users (paginated)
GET    /users/{id}                  Single user detail
PATCH  /users/{id}/active           Activate/deactivate a user account
PATCH  /users/{id}/superuser        Grant/revoke admin privileges
GET    /audit-logs                  Browse the audit trail (filterable)
GET    /notification-logs           Browse notification delivery history
GET    /deletions                   View the GDPR deletion queue
────────────────────────────────────────────────────────────────────────────

Design notes
────────────
• Every mutating admin action (deactivate user, grant superuser) writes its
  own AuditLog row so admin actions are themselves auditable — there is no
  way to silently flip is_superuser without a permanent record of who did it.
• An admin can never deactivate or modify their own account through these
  endpoints — prevents accidental lockout and forces a second admin or
  direct DB access for that specific edge case.
• List endpoints are capped at limit<=100 and always paginated; there is no
  "return everything" mode, since user/audit tables can grow unbounded.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditEvent, AuditLog
from app.auth.dependencies import get_admin_user
from app.chat.models import ChatMessage, ChatSession, Severity
from app.database import get_db
from app.models import AccountDeletion, User
from app.notifications.models import NotificationLog, NotificationStatus
from app.routers.admin_schemas import (
    AdminAuditLogOut,
    AdminDeletionQueueOut,
    AdminNotificationLogOut,
    AdminUserOut,
    AuditLogListOut,
    DeletionQueueListOut,
    MessageResponse,
    NotificationLogListOut,
    SetSuperuserRequest,
    SetUserActiveRequest,
    SystemStatsOut,
    AdminUserListOut,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ════════════════════════════════════════════════════════════════════════════
# DASHBOARD STATS
# ════════════════════════════════════════════════════════════════════════════
@router.get("/stats", response_model=SystemStatsOut)
async def get_system_stats(
    _admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
) -> SystemStatsOut:
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    active_users = (
        await db.execute(select(func.count()).select_from(User).where(User.is_active.is_(True)))
    ).scalar_one()
    verified_users = (
        await db.execute(
            select(func.count()).select_from(User).where(User.is_email_verified.is_(True))
        )
    ).scalar_one()
    pending_deletion = (
        await db.execute(
            select(func.count())
            .select_from(AccountDeletion)
            .where(
                AccountDeletion.cancelled_at.is_(None),
                AccountDeletion.completed_at.is_(None),
            )
        )
    ).scalar_one()

    total_sessions = (
        await db.execute(select(func.count()).select_from(ChatSession))
    ).scalar_one()
    total_messages = (
        await db.execute(select(func.count()).select_from(ChatMessage))
    ).scalar_one()
    sessions_24h = (
        await db.execute(
            select(func.count())
            .select_from(ChatSession)
            .where(ChatSession.created_at >= last_24h)
        )
    ).scalar_one()
    messages_24h = (
        await db.execute(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.created_at >= last_24h)
        )
    ).scalar_one()

    # Notification status breakdown, last 24h
    notif_rows = (
        await db.execute(
            select(NotificationLog.status, func.count())
            .where(NotificationLog.sent_at >= last_24h)
            .group_by(NotificationLog.status)
        )
    ).all()
    notif_breakdown = {status.value if hasattr(status, "value") else str(status): count
                       for status, count in notif_rows}
    for key in ("sent", "failed", "skipped"):
        notif_breakdown.setdefault(key, 0)

    # Severity breakdown across assistant messages, last 7 days
    sev_rows = (
        await db.execute(
            select(ChatMessage.severity, func.count())
            .where(
                ChatMessage.created_at >= last_7d,
                ChatMessage.severity.is_not(None),
            )
            .group_by(ChatMessage.severity)
        )
    ).all()
    sev_breakdown = {sev.value if hasattr(sev, "value") else str(sev): count
                     for sev, count in sev_rows}
    for key in ("green", "amber", "red"):
        sev_breakdown.setdefault(key, 0)

    return SystemStatsOut(
        total_users=total_users,
        active_users=active_users,
        verified_users=verified_users,
        users_pending_deletion=pending_deletion,
        total_chat_sessions=total_sessions,
        total_chat_messages=total_messages,
        sessions_last_24h=sessions_24h,
        messages_last_24h=messages_24h,
        notification_logs_last_24h=notif_breakdown,
        severity_breakdown_last_7d=sev_breakdown,
    )


# ════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════
@router.get("/users", response_model=AdminUserListOut)
async def list_users(
    _admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
    search: str | None = Query(default=None, description="Search by email or name"),
    is_active: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> AdminUserListOut:
    stmt = select(User)
    count_stmt = select(func.count()).select_from(User)

    if search:
        pattern = f"%{search.lower()}%"
        condition = func.lower(User.email).like(pattern) | func.lower(User.full_name).like(
            pattern
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
        count_stmt = count_stmt.where(User.is_active == is_active)

    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)
    users = (await db.execute(stmt)).scalars().all()

    # Enrich with chat session counts and pending-deletion flags
    out: list[AdminUserOut] = []
    for u in users:
        session_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatSession)
                .where(ChatSession.user_id == u.id)
            )
        ).scalar_one()
        pending = (
            await db.execute(
                select(AccountDeletion).where(
                    AccountDeletion.user_id == u.id,
                    AccountDeletion.cancelled_at.is_(None),
                    AccountDeletion.completed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        out.append(
            AdminUserOut(
                id=u.id,
                email=u.email,
                full_name=u.full_name,
                auth_provider=u.auth_provider,
                is_active=u.is_active,
                is_email_verified=u.is_email_verified,
                is_superuser=u.is_superuser,
                created_at=u.created_at,
                last_login_at=u.last_login_at,
                chat_session_count=session_count,
                has_pending_deletion=pending is not None,
            )
        )

    return AdminUserListOut(users=out, total=total, offset=offset, limit=limit)


@router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user_detail(
    user_id: uuid.UUID,
    _admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    session_count = (
        await db.execute(
            select(func.count()).select_from(ChatSession).where(ChatSession.user_id == user.id)
        )
    ).scalar_one()
    pending = (
        await db.execute(
            select(AccountDeletion).where(
                AccountDeletion.user_id == user.id,
                AccountDeletion.cancelled_at.is_(None),
                AccountDeletion.completed_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    return AdminUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        auth_provider=user.auth_provider,
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        is_superuser=user.is_superuser,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        chat_session_count=session_count,
        has_pending_deletion=pending is not None,
    )


@router.patch("/users/{user_id}/active", response_model=MessageResponse)
async def set_user_active(
    user_id: uuid.UUID,
    body: SetUserActiveRequest,
    admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    if user_id == admin.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You cannot change your own active status. Ask another admin.",
        )

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    previous = user.is_active
    user.is_active = body.is_active

    event = (
        AuditEvent.admin_user_activated
        if body.is_active
        else AuditEvent.admin_user_deactivated
    )
    db.add(
        AuditLog.record(
            event,
            user_id=user.id,
            detail=(
                f"Admin {admin.email} set is_active={body.is_active} "
                f"(was {previous}). Reason: {body.reason or 'not provided'}"
            ),
        )
    )
    await db.commit()

    action = "activated" if body.is_active else "deactivated"
    return MessageResponse(message=f"User {user.email} has been {action}.")


@router.patch("/users/{user_id}/superuser", response_model=MessageResponse)
async def set_user_superuser(
    user_id: uuid.UUID,
    body: SetSuperuserRequest,
    admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    if user_id == admin.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You cannot change your own admin status. Ask another admin.",
        )

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    previous = user.is_superuser
    user.is_superuser = body.is_superuser

    event = (
        AuditEvent.admin_superuser_granted
        if body.is_superuser
        else AuditEvent.admin_superuser_revoked
    )
    db.add(
        AuditLog.record(
            event,
            user_id=user.id,
            detail=(
                f"Admin {admin.email} set is_superuser={body.is_superuser} (was {previous})"
            ),
        )
    )
    await db.commit()

    action = "granted" if body.is_superuser else "revoked"
    return MessageResponse(message=f"Admin privileges {action} for {user.email}.")


# ════════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ════════════════════════════════════════════════════════════════════════════
@router.get("/audit-logs", response_model=AuditLogListOut)
async def list_audit_logs(
    _admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
    event: str | None = Query(default=None, description="Filter by event type"),
    user_id: uuid.UUID | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> AuditLogListOut:
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)

    if event:
        stmt = stmt.where(AuditLog.event == event)
        count_stmt = count_stmt.where(AuditLog.event == event)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
        count_stmt = count_stmt.where(AuditLog.user_id == user_id)

    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(AuditLog.occurred_at.desc()).offset(offset).limit(limit)
    logs = (await db.execute(stmt)).scalars().all()

    # Best-effort email lookup (None for erased users — by design)
    out: list[AdminAuditLogOut] = []
    for log in logs:
        email = None
        if log.user_id:
            user = (
                await db.execute(select(User.email).where(User.id == log.user_id))
            ).scalar_one_or_none()
            email = user
        out.append(
            AdminAuditLogOut(
                id=log.id,
                user_id=log.user_id,
                user_email=email,
                event=log.event,
                detail=log.detail,
                ip_address=log.ip_address,
                occurred_at=log.occurred_at,
            )
        )

    return AuditLogListOut(logs=out, total=total, offset=offset, limit=limit)


# ════════════════════════════════════════════════════════════════════════════
# NOTIFICATION LOG
# ════════════════════════════════════════════════════════════════════════════
@router.get("/notification-logs", response_model=NotificationLogListOut)
async def list_notification_logs(
    _admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
    status_filter: NotificationStatus | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> NotificationLogListOut:
    stmt = select(NotificationLog)
    count_stmt = select(func.count()).select_from(NotificationLog)

    if status_filter:
        stmt = stmt.where(NotificationLog.status == status_filter)
        count_stmt = count_stmt.where(NotificationLog.status == status_filter)

    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(NotificationLog.sent_at.desc()).offset(offset).limit(limit)
    logs = (await db.execute(stmt)).scalars().all()

    return NotificationLogListOut(
        logs=[AdminNotificationLogOut.model_validate(log) for log in logs],
        total=total,
        offset=offset,
        limit=limit,
    )


# ════════════════════════════════════════════════════════════════════════════
# GDPR DELETION QUEUE
# ════════════════════════════════════════════════════════════════════════════
@router.get("/deletions", response_model=DeletionQueueListOut)
async def list_deletion_queue(
    _admin: Annotated[User, Depends(get_admin_user)],
    db: AsyncSession = Depends(get_db),
    include_completed: bool = Query(default=False),
    include_cancelled: bool = Query(default=False),
) -> DeletionQueueListOut:
    stmt = select(AccountDeletion)
    if not include_completed:
        stmt = stmt.where(AccountDeletion.completed_at.is_(None))
    if not include_cancelled:
        stmt = stmt.where(AccountDeletion.cancelled_at.is_(None))
    stmt = stmt.order_by(AccountDeletion.execute_after.asc())

    deletions = (await db.execute(stmt)).scalars().all()

    out: list[AdminDeletionQueueOut] = []
    for d in deletions:
        # user_id stays valid (anonymised, not hard-deleted) until execution
        user = (
            await db.execute(select(User.email).where(User.id == d.user_id))
        ).scalar_one_or_none()
        out.append(
            AdminDeletionQueueOut(
                id=d.id,
                user_id=d.user_id,
                user_email=user,
                requested_at=d.requested_at,
                execute_after=d.execute_after,
                cancelled_at=d.cancelled_at,
                completed_at=d.completed_at,
            )
        )

    return DeletionQueueListOut(deletions=out, total=len(out))
