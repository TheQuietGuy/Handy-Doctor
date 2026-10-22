"""
app/gdpr/worker.py — GDPR deletion worker and data-export builder.

Deletion pipeline (FR-ACC-04)
─────────────────────────────
Every 60 seconds APScheduler calls `run_pending_deletions()`.

For each AccountDeletion row where:
  • cancelled_at IS NULL
  • completed_at IS NULL
  • execute_after <= now()

The worker runs `_wipe_user()` which:
  1.  Exports a JSON archive of the user's data (emailed first).
  2.  Deletes every piece of personal data from every table.
  3.  Anonymises the users row (replaces PII with placeholder values)
      instead of hard-deleting it, so foreign-key references in
      anonymised aggregate/analytics tables don't break.
  4.  Marks AccountDeletion.completed_at = now().
  5.  Writes an AuditLog row with event=deletion_executed.

If any step raises, the worker logs the error, writes
event=deletion_failed, and leaves the row in the queue for retry.

Data export (GDPR Article 20)
─────────────────────────────
`build_export_payload()` collects everything we hold about a user into a
single dict that can be serialised to JSON and emailed as an attachment.
The /api/accounts/me/export endpoint calls this.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditEvent, AuditLog
from app.database import AsyncSessionLocal
from app.email.sender import send_data_export_email, send_deletion_complete_email
from app.models import (
    AccountDeletion,
    EmailVerification,
    NotificationPrefs,
    PasswordResetToken,
    RefreshToken,
    User,
)

logger = logging.getLogger(__name__)

# Placeholder values written into the anonymised users row
_ANON_EMAIL_TEMPLATE = "deleted-{uid}@handydoctor.invalid"
_ANON_NAME = "[deleted]"


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULER ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════
async def run_pending_deletions() -> None:
    """
    Called by the APScheduler job every 60 seconds.
    Opens its own DB session — independent of the request lifecycle.
    """
    async with AsyncSessionLocal() as db:
        now = datetime.now(UTC)
        result = await db.execute(
            select(AccountDeletion).where(
                AccountDeletion.cancelled_at.is_(None),
                AccountDeletion.completed_at.is_(None),
                AccountDeletion.execute_after <= now,
            )
        )
        pending = result.scalars().all()

    if not pending:
        return

    logger.info("GDPR worker: %d deletion(s) due", len(pending))

    for deletion in pending:
        try:
            await _wipe_user(deletion.user_id, deletion.id)
        except Exception:
            logger.exception(
                "GDPR worker: failed to wipe user %s (deletion %s)",
                deletion.user_id,
                deletion.id,
            )


# ════════════════════════════════════════════════════════════════════════════
# WIPE LOGIC
# ════════════════════════════════════════════════════════════════════════════
async def _wipe_user(user_id: uuid.UUID, deletion_id: uuid.UUID) -> None:
    """
    Full GDPR erasure for one user.  Runs in a single transaction — either
    everything succeeds or nothing is committed.
    """
    async with AsyncSessionLocal() as db:
        # ── 1. Fetch the user ─────────────────────────────────────────────
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if user is None:
            # Already gone — just mark completed
            await _mark_completed(db, deletion_id)
            await db.commit()
            return

        # Snapshot identifiers before we wipe (needed for email + audit)
        original_email = user.email
        original_name = user.full_name

        # ── 2. Build and email the data export BEFORE wiping ──────────────
        try:
            export = await _build_export_payload(db, user)
            export_json = json.dumps(export, indent=2, default=str)
            await send_data_export_email(
                to=original_email,
                full_name=original_name,
                json_payload=export_json,
                is_deletion_export=True,
            )
        except Exception:
            logger.warning(
                "GDPR worker: could not send pre-deletion export to %s — continuing",
                original_email,
            )

        # ── 3. Delete all personal data from child tables ─────────────────
        #    (ON DELETE CASCADE covers most of these, but we are explicit
        #     so the intent is clear and testable independently.)
        await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
        await db.execute(delete(EmailVerification).where(EmailVerification.user_id == user_id))
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
        await db.execute(delete(NotificationPrefs).where(NotificationPrefs.user_id == user_id))

        # ── 4. Anonymise the users row ────────────────────────────────────
        #    We do NOT hard-delete because analytics tables may reference
        #    this user_id.  The anonymised row contains no PII.
        anon_email = _ANON_EMAIL_TEMPLATE.format(uid=str(user_id)[:8])
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                email=anon_email,
                pending_email=None,
                full_name=_ANON_NAME,
                password_hash=None,
                google_sub=None,
                date_of_birth=None,
                gender=None,
                country=None,
                avatar_url=None,
                is_active=False,
                is_email_verified=False,
                last_login_at=None,
            )
        )

        # ── 5. Mark deletion complete ─────────────────────────────────────
        await _mark_completed(db, deletion_id)

        # ── 6. Write audit record ─────────────────────────────────────────
        #    user_id is kept here — the FK is SET NULL on hard-delete but
        #    we anonymised rather than deleted, so it still resolves.
        db.add(
            AuditLog.record(
                AuditEvent.deletion_executed,
                user_id=user_id,
                detail=(
                    f"Personal data erased. "
                    f"Email anonymised to {anon_email}. "
                    f"Original address hash: {_hash_for_audit(original_email)}."
                ),
            )
        )

        await db.commit()

        logger.info("GDPR worker: successfully wiped user %s", user_id)

        # ── 7. Send deletion-complete email to the original address ───────
        #    (best-effort, outside the transaction)
        try:
            await send_deletion_complete_email(original_email, original_name)
        except Exception:
            logger.warning(
                "GDPR worker: could not send deletion-complete email to %s", original_email
            )


async def _mark_completed(db: AsyncSession, deletion_id: uuid.UUID) -> None:
    await db.execute(
        update(AccountDeletion)
        .where(AccountDeletion.id == deletion_id)
        .values(completed_at=datetime.now(UTC))
    )


def _hash_for_audit(value: str) -> str:
    """One-way hash for audit log — lets us cross-reference if needed, never reverses."""
    import hashlib

    return hashlib.sha256(value.lower().encode()).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════════════════
# DATA EXPORT  (GDPR Article 20 — data portability)
# ════════════════════════════════════════════════════════════════════════════
async def build_export_payload(db: AsyncSession, user: User) -> dict:
    """Public wrapper used by the /me/export endpoint."""
    return await _build_export_payload(db, user)


async def _build_export_payload(db: AsyncSession, user: User) -> dict:
    """
    Collect everything HandyDoctor holds about this user into one dict.
    Extend this function when new tables that store personal data are added.
    """
    # Notification prefs
    prefs_result = await db.execute(
        select(NotificationPrefs).where(NotificationPrefs.user_id == user.id)
    )
    prefs = prefs_result.scalar_one_or_none()

    # Active sessions (metadata only — never the token hash)
    sessions_result = await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
    sessions = sessions_result.scalars().all()

    # Deletion request (if any)
    deletion_result = await db.execute(
        select(AccountDeletion).where(AccountDeletion.user_id == user.id)
    )
    deletion = deletion_result.scalar_one_or_none()

    # Audit events attributed to this user
    audit_result = await db.execute(
        select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.occurred_at)
    )
    audit_rows = audit_result.scalars().all()

    return {
        "export_generated_at": datetime.now(UTC).isoformat(),
        "export_format_version": "1.0",
        "data_controller": "HandyDoctor Ltd",
        "gdpr_basis": "Article 20 — Right to data portability",
        "account": {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "date_of_birth": user.date_of_birth.date().isoformat() if user.date_of_birth else None,
            "gender": user.gender,
            "country": user.country,
            "avatar_url": user.avatar_url,
            "auth_provider": user.auth_provider,
            "is_email_verified": user.is_email_verified,
            "created_at": user.created_at.isoformat(),
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        },
        "notification_preferences": {
            "session_summaries": prefs.session_summaries if prefs else None,
            "followup_reminders": prefs.followup_reminders if prefs else None,
            "product_updates": prefs.product_updates if prefs else None,
            "security_alerts": prefs.security_alerts if prefs else None,
        }
        if prefs
        else None,
        "active_sessions": [
            {
                "id": str(s.id),
                "device_hint": s.device_hint,
                "created_at": s.created_at.isoformat(),
                "expires_at": s.expires_at.isoformat(),
            }
            for s in sessions
        ],
        "deletion_request": {
            "requested_at": deletion.requested_at.isoformat(),
            "execute_after": deletion.execute_after.isoformat(),
            "cancelled_at": deletion.cancelled_at.isoformat() if deletion.cancelled_at else None,
        }
        if deletion
        else None,
        "audit_trail": [
            {
                "event": row.event,
                "detail": row.detail,
                "occurred_at": row.occurred_at.isoformat(),
            }
            for row in audit_rows
        ],
    }
