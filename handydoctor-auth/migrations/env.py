"""
migrations/env.py — Async Alembic environment for HandyDoctor.

Key design decisions
────────────────────
• Reads DATABASE_URL from the environment (via app.config.settings) so the
  same alembic.ini works in every deployment context.

• Uses asyncpg (async) for the actual connection — required by SQLAlchemy's
  async engine.  The standard synchronous Alembic runner is wrapped with
  asyncio.run() so the CLI still works normally.

• Imports all models explicitly so autogenerate can detect new tables and
  column changes without needing --include-schemas flags.

• compare_type=True — Alembic will detect column type changes (e.g. String
  length changes) in addition to structural additions/removals.

• compare_server_default=True — detects server-side default changes.
"""

import asyncio
import sys
from pathlib import Path
from logging.config import fileConfig

# ── Ensure the project root is on sys.path ────────────────────────────────────
# This lets env.py import `app.*` regardless of the working directory from
# which `alembic` is invoked (e.g. docker exec, CI runner, scripts/ dir).
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.database import Base

# ── Import all models so autogenerate can see them ────────────────────────────
# When adding a new model file, add its import here.
from app.models import (          # noqa: F401 — side-effect imports
    User,
    RefreshToken,
    EmailVerification,
    PasswordResetToken,
    AccountDeletion,
    NotificationPrefs,
)
from app.audit import AuditLog    # noqa: F401
from app.chat.models import (     # noqa: F401
    ChatSession,
    ChatMessage,
)
from app.notifications.models import NotificationLog  # noqa: F401

# ── Alembic Config object ─────────────────────────────────────────────────────
config = context.config

# Inject the database URL from settings (overrides any value in alembic.ini)
config.set_main_option("sqlalchemy.url", settings.database_url)

# Configure Python logging from the alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata target for --autogenerate comparisons
target_metadata = Base.metadata


# ── Offline mode (no live DB connection — generates raw SQL) ──────────────────
def run_migrations_offline() -> None:
    """
    Emit SQL to stdout/file without connecting to the database.
    Useful for reviewing migrations before applying them, or for
    environments where direct DB access is restricted.

    Run with:  alembic upgrade head --sql

    The async driver prefix (+asyncpg / +aiosqlite) is stripped because
    offline mode uses SQLAlchemy's synchronous dialect rendering — no
    actual connection is made.
    """
    url = config.get_main_option("sqlalchemy.url")
    # Strip async driver prefix — offline mode doesn't make real connections
    sync_url = url.replace("+asyncpg", "").replace("+aiosqlite", "")
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode (live DB connection) ─────────────────────────────────────────
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        # Render ALTER TABLE statements rather than drop/recreate
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,    # single-use connection for migrations
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for the Alembic CLI in online mode."""
    asyncio.run(run_async_migrations())


# ── Dispatch ──────────────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
