"""add_notification_logs

Revision ID: 0004
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id",    UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "notification_type",
            sa.Enum("session_summary", "followup_reminder",
                    name="notificationtype"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("sent", "failed", "skipped", name="notificationstatus"),
            nullable=False,
        ),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"],    ["users.id"],         ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_notification_logs_user_id",    "notification_logs", ["user_id"])
    op.create_index("ix_notification_logs_session_id", "notification_logs", ["session_id"])
    op.create_index("ix_notification_logs_type",       "notification_logs", ["notification_type"])
    op.create_index("ix_notification_logs_sent_at",    "notification_logs", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_notification_logs_sent_at",    "notification_logs")
    op.drop_index("ix_notification_logs_type",       "notification_logs")
    op.drop_index("ix_notification_logs_session_id", "notification_logs")
    op.drop_index("ix_notification_logs_user_id",    "notification_logs")
    op.drop_table("notification_logs")
    op.execute("DROP TYPE IF EXISTS notificationtype")
    op.execute("DROP TYPE IF EXISTS notificationstatus")
