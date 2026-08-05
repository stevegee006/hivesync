"""Declarative base and shared column mixins.

All timestamps are stored as UTC. SQLite has no native timezone-aware type, so
the discipline is: aware datetimes in Python, UTC on the way in, never a naive
value. SPEC section 9 makes scheduling timezone aware, and mixing the two is how
that goes wrong.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
