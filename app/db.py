"""Database engine and session management.

SQLite needs three settings that are not defaults and that the spec depends on:

- WAL journaling, because APScheduler's jobstore and a running sync write to the
  same file. Without it, concurrent access produces "database is locked".
- A busy timeout, for the same reason.
- foreign_keys=ON, which SQLite leaves off by default. Without it every
  ON DELETE RESTRICT in the schema is silently ignored.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.config import Settings


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_db_engine(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        # The app serves requests from a threadpool and runs scheduled jobs on
        # other threads. Sessions are per-request, so sharing is safe.
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
        future=True,
    )
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope for code outside the request cycle, such as startup."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def database_ok(engine: Engine) -> tuple[bool, str | None]:
    """Cheap liveness probe for /api/health. Never raises."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return False, type(exc).__name__
    return True, None


def get_session(request: Request) -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    The Request annotation is load bearing: FastAPI resolves parameters by type,
    and anything it does not recognise as a Request becomes a query parameter,
    which turns every route using this into a 422.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()
