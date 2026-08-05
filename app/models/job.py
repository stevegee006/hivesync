"""Jobs and their run history.

Two things here are worth reading before changing:

1. source_path is named symmetrically with dest_path. SPEC section 4 calls it
   source_subpath, which describes the same concept two different ways on the
   two sides of one job.

2. The partial unique index on JobRun is what makes "one running JobRun per Job,
   enforced at the DB level" from SPEC section 6.2 true rather than aspirational.
   Application-level checks race; this does not. SQLite supports partial indexes,
   so the predicate is evaluated on the stored status value.

"source" and "dest" are positional labels, equivalent to rclone's path1 and
path2. Direction alone decides which way bytes move, so a dest_to_source job
writes to the connection named "source". See CLAUDE.md.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UtcDateTime, str_enum
from app.models.connection import Connection
from app.models.filter_preset import FilterPreset, job_filter_preset


class Engine(enum.StrEnum):
    rclone = "rclone"
    lftp = "lftp"


class Direction(enum.StrEnum):
    source_to_dest = "source_to_dest"
    dest_to_source = "dest_to_source"
    bidirectional = "bidirectional"


class DeleteMode(enum.StrEnum):
    none = "none"
    delete = "delete"
    archive = "archive"


class ArchiveLayout(enum.StrEnum):
    timestamped_dir = "timestamped_dir"
    suffix = "suffix"


class CompareMode(enum.StrEnum):
    mtime_size = "mtime_size"
    checksum = "checksum"
    size_only = "size_only"


class ConflictResolve(enum.StrEnum):
    newer = "newer"
    older = "older"
    larger = "larger"
    smaller = "smaller"
    path1 = "path1"
    path2 = "path2"
    none = "none"


class NotifyOn(enum.StrEnum):
    never = "never"
    failure = "failure"
    always = "always"


class RunTrigger(enum.StrEnum):
    manual = "manual"
    schedule = "schedule"
    api = "api"


class RunMode(enum.StrEnum):
    dry_run = "dry_run"
    live = "live"


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"
    skipped = "skipped"


class ChangeAction(enum.StrEnum):
    new = "new"
    updated = "updated"
    deleted = "deleted"
    archived = "archived"
    unchanged = "unchanged"
    conflict = "conflict"
    error = "error"


class ChangeSide(enum.StrEnum):
    source = "source"
    dest = "dest"


class Job(Base, TimestampMixin):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # RESTRICT so a connection cannot be deleted out from under a job. The API
    # returns 409 for this case, but the constraint is what makes it true for
    # every other code path, including the scheduler.
    source_connection_id: Mapped[int] = mapped_column(
        ForeignKey("connection.id", ondelete="RESTRICT"), nullable=False
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dest_connection_id: Mapped[int] = mapped_column(
        ForeignKey("connection.id", ondelete="RESTRICT"), nullable=False
    )
    dest_path: Mapped[str] = mapped_column(Text, nullable=False, default="")

    source_connection: Mapped[Connection] = relationship(foreign_keys=[source_connection_id])
    dest_connection: Mapped[Connection] = relationship(foreign_keys=[dest_connection_id])

    engine: Mapped[Engine] = mapped_column(
        str_enum(Engine, length=16), nullable=False, default=Engine.rclone
    )
    direction: Mapped[Direction] = mapped_column(
        str_enum(Direction), nullable=False, default=Direction.source_to_dest
    )

    delete_mode: Mapped[DeleteMode] = mapped_column(
        str_enum(DeleteMode, length=16), nullable=False, default=DeleteMode.none
    )
    # Null means "derive the sibling default at run time". SPEC section 7.1.
    archive_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_layout: Mapped[ArchiveLayout] = mapped_column(
        str_enum(ArchiveLayout), nullable=False, default=ArchiveLayout.timestamped_dir
    )
    archive_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # include[], exclude[], min_size, max_age. Preset membership is a real
    # relationship, not a list of ids in here. See filter_preset.py.
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    filter_presets: Mapped[list[FilterPreset]] = relationship(
        secondary=job_filter_preset, lazy="selectin"
    )

    compare_mode: Mapped[CompareMode] = mapped_column(
        str_enum(CompareMode), nullable=False, default=CompareMode.mtime_size
    )
    # rclone duration string. Default 1s because a NAS clock drifting or a
    # coarser timestamp granularity otherwise re-transfers unchanged files
    # forever, and that symptom is the diagnostic. SPEC section 11.1.
    modify_window: Mapped[str] = mapped_column(String(16), nullable=False, default="1s")

    transfers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bwlimit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The delete brake. Never optional, never bypassed. SPEC section 6.4.
    max_delete_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    conflict_resolve: Mapped[ConflictResolve] = mapped_column(
        str_enum(ConflictResolve, length=16), nullable=False, default=ConflictResolve.newer
    )

    schedule_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notify_on: Mapped[NotifyOn] = mapped_column(
        str_enum(NotifyOn, length=16), nullable=False, default=NotifyOn.failure
    )

    # False until a successful bisync --resync. Never set implicitly by a run.
    bisync_initialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    runs: Mapped[list[JobRun]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"Job(id={self.id!r}, name={self.name!r}, direction={self.direction!r})"


class JobRun(Base):
    __tablename__ = "job_run"
    __table_args__ = (
        # At most one queued or running row per job. A scheduled trigger that
        # collides records a skipped run instead, which does not match the
        # predicate and so does not conflict.
        Index(
            "uq_job_run_active_per_job",
            "job_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_job_run_job_started", "job_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Runs are history belonging to the job, so they go when the job goes.
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job: Mapped[Job] = relationship(back_populates="runs")

    trigger: Mapped[RunTrigger] = mapped_column(str_enum(RunTrigger, length=16), nullable=False)
    mode: Mapped[RunMode] = mapped_column(str_enum(RunMode, length=16), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, length=16), nullable=False, default=RunStatus.queued
    )

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    files_transferred: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_archived: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Redacted copy of the argv that ran. Never the real one. SPEC section 6.1.
    command_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    changes: Mapped[list[JobRunChange]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"JobRun(id={self.id!r}, job_id={self.job_id!r}, status={self.status!r})"


class JobRunChange(Base):
    __tablename__ = "job_run_change"
    __table_args__ = (Index("ix_job_run_change_run_action", "run_id", "action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("job_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run: Mapped[JobRun] = relationship(back_populates="changes")

    action: Mapped[ChangeAction] = mapped_column(str_enum(ChangeAction, length=16), nullable=False)
    side: Mapped[ChangeSide] = mapped_column(str_enum(ChangeSide, length=16), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"JobRunChange(id={self.id!r}, action={self.action!r}, path={self.path!r})"
