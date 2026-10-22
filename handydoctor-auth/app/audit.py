"""
Append-only audit table for GDPR-relevant events.
Added as a second Alembic migration (0002).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class AuditEvent(enum.StrEnum):
    # Account lifecycle
    account_registered = "account_registered"
    email_verified = "email_verified"
    login_success = "login_success"
    login_failed = "login_failed"
    logout = "logout"
    password_changed = "password_changed"
    email_changed = "email_changed"

    # GDPR deletion pipeline
    deletion_requested = "deletion_requested"
    deletion_cancelled = "deletion_cancelled"
    deletion_executed = "deletion_executed"  # personal data wiped
    deletion_failed = "deletion_failed"  # worker error — retryable

    # GDPR data export (Article 20)
    data_export_requested = "data_export_requested"
    data_export_sent = "data_export_sent"

    # Admin actions (performed by a superuser against another user's account)
    admin_user_activated = "admin_user_activated"
    admin_user_deactivated = "admin_user_deactivated"
    admin_superuser_granted = "admin_superuser_granted"
    admin_superuser_revoked = "admin_superuser_revoked"


class AuditLog(Base):
    """
    Append-only audit log.  Rows are never updated or deleted — they form a
    tamper-evident trail of security and GDPR events.

    user_id is nullable: after erasure the user row no longer exists, but we
    keep a redacted log entry (no email, no name) for compliance evidence.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable post-erasure
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event: Mapped[AuditEvent] = mapped_column(Enum(AuditEvent), nullable=False, index=True)
    # Human-readable detail — never contains passwords or tokens
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    @classmethod
    def record(
        cls,
        event: AuditEvent,
        *,
        user_id: uuid.UUID | None = None,
        detail: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> "AuditLog":
        return cls(
            user_id=user_id,
            event=event,
            detail=detail,
            ip_address=ip_address,
            user_agent=user_agent,
        )
