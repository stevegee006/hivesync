"""Declarative base and shared column mixins.

All timestamps are stored as UTC. SQLite has no native timezone-aware type, so
the discipline is: aware datetimes in Python, UTC on the way in, never a naive
value. SPEC section 9 makes scheduling timezone aware, and mixing the two is how
that goes wrong.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware datetime that stays aware through a SQLite round trip.

    SQLite has no native timezone-aware type, so a plain DateTime(timezone=True)
    stores an aware value and hands back a naive one. Any later comparison against
    `utcnow()` then raises "can't subtract offset-naive and offset-aware
    datetimes", which is a runtime error in whichever code path happens to compare
    first. Scheduling is timezone aware per SPEC section 9, so this would keep
    resurfacing.

    Values are normalised to UTC going in and reattached to UTC coming out. A
    naive value going in is assumed to be UTC rather than rejected, so a fixture
    or a raw SQL insert cannot poison a row.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"Expected a datetime, got {type(value).__name__}.")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        loaded: datetime = value
        if loaded.tzinfo is None:
            return loaded.replace(tzinfo=UTC)
        return loaded.astimezone(UTC)


def str_enum(enum_cls: type[enum.Enum], length: int = 32) -> SAEnum:
    """Store a Python enum as a CHECK-constrained VARCHAR holding its value.

    values_callable is explicit because SQLAlchemy persists the enum member name
    by default. Every enum here has name == value today, but a partial index in
    job.py depends on the stored literal, so the intent is pinned down rather
    than left to a coincidence.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
