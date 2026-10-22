"""add_admin_audit_events

Revision ID: 0005
Create Date: 2026-05-23

Adds four new values to the existing PostgreSQL `auditevent` enum type:
  admin_user_activated, admin_user_deactivated,
  admin_superuser_granted, admin_superuser_revoked

PostgreSQL requires ALTER TYPE ... ADD VALUE for enum extension and (prior
to PG 12) cannot run inside a transaction block together with other DDL —
each ADD VALUE is executed as its own statement with autocommit semantics
to stay compatible with older PostgreSQL versions still in the wild.

SQLite has no native enum type (it's just a CHECK constraint via
SQLAlchemy's Enum), so this migration is a no-op for SQLite test runs —
the application's enum.StrEnum class is the source of truth there and
already includes these values.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_NEW_VALUES = [
    "admin_user_activated",
    "admin_user_deactivated",
    "admin_superuser_granted",
    "admin_superuser_revoked",
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (used in tests) has no native enum — nothing to alter.
        return

    # Each ALTER TYPE ... ADD VALUE must run outside an explicit transaction
    # block on PostgreSQL < 12. Alembic's autocommit_block() handles this.
    for value in _NEW_VALUES:
        with op.get_context().autocommit_block():
            op.execute(f"ALTER TYPE auditevent ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # A downgrade would require recreating the type and remapping every
    # existing row — out of scope for a reversible migration here.
    # If you must roll back, restore from a pre-migration backup instead.
    pass
