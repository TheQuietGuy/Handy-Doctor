"""add_care_levels

Revision ID: 0007
Create Date: 2026-11-07

Adds the five-level care scale to chat messages:
  care_level      self_care | pharmacy | gp | urgent | emergency
  triage_reasons  JSON list of red-flag rule hits that set the level
  context         JSON guided-interview answers (on user messages)
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat_messages") as batch:
        batch.add_column(sa.Column("care_level", sa.String(16), nullable=True))
        batch.add_column(sa.Column("triage_reasons", sa.Text(), nullable=True))
        batch.add_column(sa.Column("context", sa.Text(), nullable=True))
    # Backfill from the old three-colour severity
    op.execute(
        "UPDATE chat_messages SET care_level = CASE severity "
        "WHEN 'green' THEN 'self_care' WHEN 'amber' THEN 'gp' WHEN 'red' THEN 'emergency' END "
        "WHERE role = 'assistant' AND severity IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_column("context")
        batch.drop_column("triage_reasons")
        batch.drop_column("care_level")
