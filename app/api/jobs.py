"""Job and run endpoints. SPEC section 12.

Triggering a run returns immediately with the run's id. The work happens on a
worker thread, because a plan over a real tree takes minutes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db import get_session
from app.engines.base import EngineError
from app.jobs import planner
from app.jobs.events import broker
from app.models import (
    Connection,
    FilterPreset,
    Job,
    JobRun,
    JobRunChange,
    RunMode,
    RunStatus,
    RunTrigger,
)
from app.schemas.job import (
    JobCreate,
    JobRead,
    JobUpdate,
    RunChangeRead,
    RunRead,
    RunRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])

_SCALARS = (
    "name",
    "enabled",
    "source_connection_id",
    "source_path",
    "dest_connection_id",
    "dest_path",
    "engine",
    "direction",
    "delete_mode",
    "archive_base",
    "archive_layout",
    "archive_retention_days",
    "compare_mode",
    "modify_window",
    "transfers",
    "checkers",
    "bwlimit",
    "max_delete_pct",
    "conflict_resolve",
    "schedule_cron",
    "timezone",
    "timeout_seconds",
    "notify_on",
)


def describe(job: Job) -> str:
    """The plain English summary SPEC section 13 requires on the Review step.

    Rendered from the saved job rather than the form, so it describes what will
    actually happen rather than what someone meant to type.
    """
    source = job.source_connection
    dest = job.dest_connection
    reading, writing = (dest, source) if job.direction.value == "dest_to_source" else (source, dest)
    read_path, write_path = (
        (job.dest_path, job.source_path)
        if job.direction.value == "dest_to_source"
        else (job.source_path, job.dest_path)
    )

    when = (
        f"On the schedule {job.schedule_cron} ({job.timezone})" if job.schedule_cron else "When run"
    )
    lead = (
        f"{when}, copy new and changed files from "
        f"{reading.name}:{read_path or '/'} to {writing.name}:{write_path or '/'}."
    )

    if job.delete_mode.value == "none":
        deletion = f" Files removed from {reading.name} are left alone on {writing.name}."
    else:
        deletion = (
            f" Files removed from {reading.name} will be deleted from {writing.name}, "
            f"up to a brake of {job.max_delete_pct}% of the destination."
        )

    back = f" Nothing will be written back to {reading.name}."
    return lead + deletion + back


def to_read(job: Job) -> JobRead:
    model = JobRead.model_validate(job)
    model.source_connection_name = job.source_connection.name if job.source_connection else None
    model.dest_connection_name = job.dest_connection.name if job.dest_connection else None
    model.filter_preset_ids = [preset.id for preset in job.filter_presets]
    model.description = describe(job)
    return model


def _apply(session: Session, job: Job, payload: JobCreate | JobUpdate) -> None:
    for name in _SCALARS:
        setattr(job, name, getattr(payload, name))
    job.filters = payload.filters.model_dump(exclude_none=True)

    if payload.filter_preset_ids:
        presets = list(
            session.scalars(
                select(FilterPreset).where(FilterPreset.id.in_(payload.filter_preset_ids))
            )
        )
        if len(presets) != len(set(payload.filter_preset_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One of the selected filter presets no longer exists.",
            )
        job.filter_presets = presets
    else:
        job.filter_presets = []


def _require_connections(session: Session, payload: JobCreate | JobUpdate) -> None:
    for field, connection_id in (
        ("source", payload.source_connection_id),
        ("destination", payload.dest_connection_id),
    ):
        if session.get(Connection, connection_id) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The {field} connection does not exist.",
            )


class FilterPresetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    builtin: bool
    rules: dict[str, Any]


@router.get("/filter-presets", response_model=list[FilterPresetRead])
def list_filter_presets(
    _user: CurrentUser, session: Session = Depends(get_session)
) -> list[FilterPresetRead]:
    """SPEC section 12. The built-ins are seeded at startup."""
    presets = session.scalars(select(FilterPreset).order_by(FilterPreset.name)).all()
    return [FilterPresetRead.model_validate(preset) for preset in presets]


@router.get("/jobs", response_model=list[JobRead])
def list_jobs(_user: CurrentUser, session: Session = Depends(get_session)) -> list[JobRead]:
    jobs = session.scalars(select(Job).order_by(Job.name)).all()
    return [to_read(job) for job in jobs]


@router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreate, _user: CurrentUser, session: Session = Depends(get_session)
) -> JobRead:
    _require_connections(session, payload)
    job = Job()
    _apply(session, job, payload)
    session.add(job)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A job named '{payload.name}' already exists.",
        ) from exc
    logger.info("Created job", extra={"job": job.name})
    return to_read(job)


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: int, _user: CurrentUser, session: Session = Depends(get_session)) -> JobRead:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    return to_read(job)


@router.patch("/jobs/{job_id}", response_model=JobRead)
def update_job(
    job_id: int,
    payload: JobUpdate,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> JobRead:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    _require_connections(session, payload)
    _apply(session, job, payload)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Another job already has that name."
        ) from exc
    return to_read(job)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, _user: CurrentUser, session: Session = Depends(get_session)) -> None:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")
    session.delete(job)
    session.commit()


@router.post("/jobs/{job_id}/run", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
def run_job(
    job_id: int,
    payload: RunRequest,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> RunRead:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job.")

    try:
        run = planner.create_run(session, job, trigger=RunTrigger.api, mode=payload.mode)
    except planner.RunConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except EngineError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # A live run plans first and refuses before writing anything if the brake
    # would be exceeded. See jobs.runner.
    if payload.mode == RunMode.live:
        request.app.state.live_runner.submit(run.id)
    else:
        request.app.state.plan_runner.submit(run.id)
    return RunRead.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
def cancel_run(
    run_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> RunRead:
    """SIGTERM, ten seconds, then SIGKILL. SPEC section 6.3.

    The grace period is not politeness: rclone removes the partial file it is
    writing when it receives SIGTERM, and cannot when it is killed.
    """
    run = session.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    if run.status not in (RunStatus.queued, RunStatus.running):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"That run already finished with status {run.status.value}.",
        )

    run.status = RunStatus.cancelled
    session.commit()
    request.app.state.live_runner.cancel(run_id)
    session.refresh(run)
    return RunRead.model_validate(run)


@router.get("/runs/{run_id}/log")
def get_run_log(
    run_id: int, _user: CurrentUser, session: Session = Depends(get_session)
) -> PlainTextResponse:
    """The raw per-run log. Redacted as it was written. SPEC section 16."""
    run = session.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    if not run.log_path:
        return PlainTextResponse("No log was captured for this run.")
    path = Path(run.log_path)
    if not path.is_file():
        return PlainTextResponse("The log file for this run is no longer on disk.")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@router.get("/runs/{run_id}/stream")
def stream_run(
    run_id: int, _user: CurrentUser, session: Session = Depends(get_session)
) -> StreamingResponse:
    """Live output over Server-Sent Events. SPEC section 12.

    Reads from the in-process broker rather than the database: a stream stays
    open for the length of a sync, and holding a SQLite session that long would
    block writers.
    """
    if session.get(JobRun, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")

    def emit() -> Iterator[str]:
        for event in broker.subscribe(run_id):
            payload = json.dumps({"kind": event.kind, "text": event.text})
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/runs", response_model=list[RunRead])
def list_runs(
    job_id: int, _user: CurrentUser, session: Session = Depends(get_session)
) -> list[RunRead]:
    return [RunRead.model_validate(run) for run in planner.latest_runs(session, job_id)]


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: int, _user: CurrentUser, session: Session = Depends(get_session)) -> RunRead:
    run = session.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")
    return RunRead.model_validate(run)


@router.get("/runs/{run_id}/changes", response_model=list[RunChangeRead])
def get_run_changes(
    run_id: int,
    _user: CurrentUser,
    action: str | None = None,
    page: int = 1,
    page_size: int = 200,
    session: Session = Depends(get_session),
) -> list[RunChangeRead]:
    if session.get(JobRun, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run.")

    query = select(JobRunChange).where(JobRunChange.run_id == run_id)
    if action:
        query = query.where(JobRunChange.action == action)
    query = (
        query.order_by(JobRunChange.action, JobRunChange.path)
        .offset(max(0, (page - 1)) * page_size)
        .limit(min(page_size, 1000))
    )
    return [RunChangeRead.model_validate(row) for row in session.scalars(query)]
