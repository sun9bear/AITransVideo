"""Alembic environment for async PostgreSQL migrations."""
import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

# Ensure gateway root is on sys.path so models/config can be imported
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import models so Alembic sees them
from models import Base  # noqa: F401
# Ensure all tables are registered to Base.metadata so autogenerate
# does not propose dropping production tables. Without these imports,
# Base.metadata only contains tables declared directly in models.py;
# tables defined in sibling modules (voice_catalog, background_tasks,
# label_tasks) would appear as "extra in DB" and autogenerate would
# emit DROP TABLE statements for them.
import voice_catalog_models  # noqa: F401  # adds VoiceCatalog / VoiceLabel
import background_task_models  # noqa: F401  # adds BackgroundTask
import label_task_models  # noqa: F401  # adds LabelTask
from config import resolve_database_url, settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# T3 fix: settings.database_url is empty by design (lazy init pattern in
# gateway/database.py). Call resolve_database_url() explicitly so Alembic
# gets the real URL built from AVT_PG_PASSWORD / AVT_DATABASE_URL — the
# standard compose setup uses AVT_PG_PASSWORD with empty AVT_DATABASE_URL,
# so reading settings.database_url directly would produce an empty URL.
# Escape % in URL for configparser (password may contain URL-encoded special chars)
config.set_main_option(
    "sqlalchemy.url",
    resolve_database_url(settings).replace("%", "%%"),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
