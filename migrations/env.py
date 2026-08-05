"""Alembic environment.

The database URL comes from app.config rather than alembic.ini, so there is one
source of truth for where the database lives.

render_as_batch is on because SQLite cannot ALTER most things in place. Without
it, a later migration that drops or alters a column fails on the target database
even though it works on Postgres.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings

# Importing the models package registers every table on Base.metadata.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which switches off every logger
    # that alembic.ini does not name. In-process that silently kills application
    # logging for the rest of the run, and the symptom is log lines simply not
    # appearing rather than an error. Found by the redaction sweep, which asserts
    # that expected content is present as well as that secrets are absent.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
settings.ensure_directories()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # SQLite ignores foreign keys unless asked, including during migrations.
        # Issued before any DML so it lands outside a transaction, where SQLite
        # honours it.
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()

        # This commit is load bearing. SQLite reports non-transactional DDL, so
        # begin_transaction above is a no-op, and pysqlite does not emit BEGIN
        # until the first DML statement. Without an explicit commit the CREATE
        # TABLE statements persist through autocommit while the INSERT into
        # alembic_version is rolled back at connection close, leaving a fully
        # built schema that Alembic believes is at revision base.
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
