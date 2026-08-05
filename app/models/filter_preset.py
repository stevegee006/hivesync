"""Reusable filter rule sets, including the built-in Synology and junk presets.

SPEC section 4 puts preset_ids inside the Job.filters JSON blob. That is a
foreign key with no constraint behind it: deleting a preset would silently
corrupt every job referencing it. The association table below makes the
relationship real, with RESTRICT on the preset side. Everything else in
Job.filters stays JSON, since include and exclude patterns need no integrity.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Column, ForeignKey, String, Table
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

job_filter_preset = Table(
    "job_filter_preset",
    Base.metadata,
    Column("job_id", ForeignKey("job.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "filter_preset_id",
        ForeignKey("filter_preset.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class FilterPreset(Base, TimestampMixin):
    __tablename__ = "filter_preset"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Built-in presets are seeded at startup and cannot be deleted through the API.
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"FilterPreset(id={self.id!r}, name={self.name!r}, builtin={self.builtin!r})"
