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

    filters: JobFilters = Field(default_factory=JobFilters)
    filter_preset_ids: list[int] = Field(default_factory=list)

    compare_mode: CompareMode = CompareMode.mtime_size
    modify_window: str = "1s"

    transfers: int | None = Field(default=None, ge=1, le=64)
    checkers: int | None = Field(default=None, ge=1, le=256)
    bwlimit: str | None = Field(default=None, max_length=32)

    max_delete_pct: int = Field(default=20, ge=0, le=100)
    conflict_resolve: ConflictResolve = ConflictResolve.newer

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

        if self.engine == Engine.lftp:
            # SPEC 2.2. The engine does not exist yet, and its constraints differ.
            raise ValueError(
                "The lftp engine is not available. It is optional and arrives in a "
                "later milestone, if at all. Use rclone."
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

        if self.delete_mode == DeleteMode.archive:
            raise ValueError(
                "Deletion archiving is not implemented yet. Choose 'none' to leave "
                "extra files alone, or 'delete' to remove them."
            )

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
