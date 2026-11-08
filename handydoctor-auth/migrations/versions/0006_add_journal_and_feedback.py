"""add_journal_and_feedback

Revision ID: 0006
Create Date: 2026-11-07

* New table `symptom_entries` (symptom journal).
* New nullable columns on `chat_messages`: feedback, feedback_comment.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "symptom_entries",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "user_id", _uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("symptom", sa.String(120), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("severity BETWEEN 1 AND 10", name="ck_symptom_severity_range"),
    )
    op.create_index("ix_symptom_entries_user_id", "symptom_entries", ["user_id"])
    op.create_index("ix_symptom_entries_recorded_at", "symptom_entries", ["recorded_at"])

    with op.batch_alter_table("chat_messages") as batch:
        batch.add_column(sa.Column("feedback", sa.String(8), nullable=True))
        batch.add_column(sa.Column("feedback_comment", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_column("feedback_comment")
        batch.drop_column("feedback")
    op.drop_index("ix_symptom_entries_recorded_at", table_name="symptom_entries")
    op.drop_index("ix_symptom_entries_user_id", table_name="symptom_entries")
    op.drop_table("symptom_entries")
