"""Job request and response models.

The interesting validation is not shape, it is intent. SPEC section 6.4 asks the
tool to refuse rather than guess, and a job is the object that eventually deletes
files, so a combination that cannot work should be impossible to save.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import (
    ArchiveLayout,
    CompareMode,
    ConflictResolve,
    DeleteMode,
    Direction,
    Engine,
    NotifyOn,
    RunMode,
    RunStatus,
    RunTrigger,
)


class JobFilters(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    min_size: str | None = None
    max_age: str | None = None


class JobBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True

    source_connection_id: int
    source_path: str = ""
    dest_connection_id: int
    dest_path: str = ""

    engine: Engine = Engine.rclone
    direction: Direction = Direction.source_to_dest

    delete_mode: DeleteMode = DeleteMode.none
    archive_base: str | None = None
    archive_layout: ArchiveLayout = ArchiveLayout.timestamped_dir
    archive_retention_days: int | None = Field(default=None, ge=1)
    continuous: bool = False
    continuous_interval_seconds: int = Field(default=60, ge=5, le=86400)
    continuous_idle_interval_seconds: int = Field(default=900, ge=5, le=86400)
    quiet_period_seconds: int = Field(default=0, ge=0, le=3600)

    filters: JobFilters = Field(default_factory=JobFilters)
    filter_preset_ids: list[int] = Field(default_factory=list)

    compare_mode: CompareMode = CompareMode.mtime_size
    modify_window: str = "1s"

    transfers: int | None = Field(default=None, ge=1, le=64)
    checkers: int | None = Field(default=None, ge=1, le=256)
    bwlimit: str | None = Field(default=None, max_length=32)

    max_delete_pct: int = Field(default=20, ge=0, le=100)
    conflict_resolve: ConflictResolve = ConflictResolve.newer
    # Opt in to bisync's own stale mount guard. See app/engines/bisync.py.
    check_access: bool = False

    schedule_cron: str | None = Field(default=None, max_length=128)
    timezone: str = "UTC"
    timeout_seconds: int | None = Field(default=None, ge=1)

    notify_on: NotifyOn = NotifyOn.failure

    @model_validator(mode="after")
    def _check_consistency(self) -> JobBase:
        # SPEC 6.4: refuse a job whose endpoints overlap. Same connection and
        # same path is a job that would sync a tree onto itself.
        if self.source_connection_id == self.dest_connection_id and (self.source_path or "").strip(
            "/"
        ) == (self.dest_path or "").strip("/"):
            raise ValueError(
                "The source and destination are the same connection and the same "
                "path, so this job would sync a directory onto itself."
            )

        if self.direction == Direction.bidirectional and self.delete_mode != DeleteMode.none:
            # bisync propagates deletions itself, driven by its listing state.
            # A separate delete mode would be a second, conflicting opinion about
            # what to remove.
            raise ValueError(
                "Bidirectional sync handles deletions itself, so the extra files "
                "setting must be 'leave them alone'. The delete brake still applies."
            )

        if self.continuous and self.direction == Direction.bidirectional:
            # bisync lists both sides and carries workdir state, so it is both
            # the most expensive thing to run on a loop and the one where a
            # mistake is hardest to undo.
            raise ValueError(
                "Continuous mode is not available for bidirectional jobs. A "
                "bidirectional run compares both sides in full and keeps its own "
                "state, so running it on a loop is expensive and harder to "
                "recover from. Use a schedule, or make this a one way job."
            )

        if self.continuous and (self.schedule_cron or "").strip():
            # Two opinions about when to run is one too many.
            raise ValueError(
                "A job is either continuous or scheduled, not both. Clear the "
                "schedule to watch continuously, or turn continuous mode off to "
                "keep the schedule."
            )

        if self.continuous and self.continuous_idle_interval_seconds < (
            self.continuous_interval_seconds
        ):
            raise ValueError(
                "The idle interval is how far continuous mode backs off when "
                "nothing is changing, so it cannot be shorter than the interval "
                "it backs off from."
            )

        # Checked before the archive refusal below, so the type of delete_mode is
        # not yet narrowed and this stays a meaningful comparison.
        if self.archive_base and self.delete_mode != DeleteMode.archive:
            raise ValueError("An archive path only applies when deletion archiving is on.")

        # A schedule that cannot be parsed must not be savable: it would sit
        # there looking configured and never fire.
        if self.schedule_cron and self.schedule_cron.strip():
            from app.jobs.cron import CronError, validate

            try:
                validate(self.schedule_cron, self.timezone)
            except CronError as exc:
                raise ValueError(str(exc)) from exc

        return self


class JobCreate(JobBase):
    pass


class JobUpdate(JobBase):
    pass


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    source_connection_id: int
    source_path: str
    dest_connection_id: int
    dest_path: str
    engine: Engine
    direction: Direction
    delete_mode: DeleteMode
    archive_base: str | None
    archive_layout: ArchiveLayout
    archive_retention_days: int | None
    filters: dict[str, Any]
    compare_mode: CompareMode
    modify_window: str
    transfers: int | None
    checkers: int | None
    bwlimit: str | None
    max_delete_pct: int
    conflict_resolve: ConflictResolve
    schedule_cron: str | None
    timezone: str
    timeout_seconds: int | None
    notify_on: NotifyOn
    bisync_initialized: bool
    continuous: bool = False
    continuous_interval_seconds: int = 60
    continuous_idle_interval_seconds: int = 900
    quiet_period_seconds: int = 0
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    source_connection_name: str | None = None
    dest_connection_name: str | None = None
    filter_preset_ids: list[int] = Field(default_factory=list)
    # Plain English, per SPEC section 13's Review step.
    description: str | None = None


class RunRequest(BaseModel):
    mode: RunMode = RunMode.dry_run


class RunChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    side: str
    path: str
    size: int | None
    message: str | None


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    trigger: RunTrigger
    mode: RunMode
    status: RunStatus
    is_resync: bool
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    files_transferred: int
    files_deleted: int
    bytes_transferred: int
    errors_count: int
    command_redacted: str | None
    summary: dict[str, Any] | None
    skip_reason: str | None
