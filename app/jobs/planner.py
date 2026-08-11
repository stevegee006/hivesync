"""Run lifecycle for a dry run.

A plan over a real NAS tree takes minutes, which is longer than an HTTP request
should hold open, so the JobRun row is created immediately and the work happens
on a worker thread. The run detail page polls.

M3 replaces the polling with Server-Sent Events and adds cancellation. Both are
additions to this shape rather than replacements for it: the run row, its status
transitions and its persisted changes are the same either way.

Concurrency is enforced by the database, not by this module. A partial unique
index allows at most one queued or running JobRun per Job, so a second trigger
fails to insert rather than racing. SPEC section 6.2.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.crypto import SecretBox
from app.engines.base import EngineError, Plan, SyncEngine
from app.engines.bisync import BisyncEngine
from app.engines.rclone import RcloneEngine, side_for
from app.jobs.events import RunEvent, broker
from app.models import (
    Direction,
    Engine,
    Job,
    JobRun,
    JobRunChange,
    RunMode,
    RunStatus,
    RunTrigger,
    utcnow,
)

logger = logging.getLogger(__name__)

# Individual change rows are capped so one pathological plan cannot write a
# million rows. The summary counts remain exact; only the listing is bounded.
MAX_PERSISTED_CHANGES = 5000


class RunConflict(Exception):
    """This job already has a run in flight. SPEC section 6.2."""


class PlannerBusy(Exception):
    """The global concurrency cap is reached."""


def engine_for(job: Job) -> SyncEngine:
    """The engine that plans this job.

    Bidirectional jobs go to bisync, which is a different command with its own
    semantics rather than a flag on `sync`. Until this branch existed, a dry run
    of a bidirectional job hit RcloneEngine and was refused, so the one mode
    that can damage both copies was the one you could not preview.
    """
    if job.engine != Engine.rclone:
        raise EngineError(
            f"The {job.engine.value} engine is not implemented. Only rclone can "
            "run jobs at present."
        )
    if job.direction == Direction.bidirectional:
        return BisyncEngine()
    return RcloneEngine()


def create_run(session: Session, job: Job, *, trigger: RunTrigger, mode: RunMode) -> JobRun:
    """Insert a queued run, or refuse because one is already active.

    The refusal comes from the database's partial unique index rather than a
    prior SELECT, so two simultaneous triggers cannot both pass the check.
    """
    run = JobRun(job_id=job.id, trigger=trigger, mode=mode, status=RunStatus.queued)
    session.add(run)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise RunConflict(
            f"'{job.name}' already has a run in progress. Wait for it to finish, "
            "or open it to see what it is doing."
        ) from exc
    return run


class PlanRunner:
    """Owns the worker pool and the sessions its jobs use.

    Held on app.state so the pool outlives a request, and so tests can drive it
    synchronously by calling run_now directly.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        box: SecretBox,
        settings: Settings,
        max_workers: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._box = box
        self._settings = settings
        # SPEC section 6.2: global concurrency cap, default 3.
        self._thread_prefix = "hivesync-run"
        self._max_workers = max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=self._thread_prefix
        )
        self._lock = threading.Lock()

    def resize(self, max_workers: int) -> None:
        """Change how many runs may go at once, without waiting for a restart.

        A ThreadPoolExecutor cannot be resized, so this swaps in a new one and
        lets the old drain: futures already running finish on the old pool's
        threads, and new work goes to the new one. `shutdown(wait=False)` is
        deliberate, because waiting here would block the request that changed
        the setting for as long as the longest sync.

        The cost is that the limit can be briefly exceeded while the old pool
        drains. That is a resource setting rather than a safety one: nothing is
        made unsafe by three transfers running where two were asked for, and the
        alternative is refusing to change it until everything is idle.
        """
        if max_workers < 1 or max_workers == self._max_workers:
            return
        with self._lock:
            old = self._pool
            self._pool = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix=self._thread_prefix
            )
            self._max_workers = max_workers
        old.shutdown(wait=False)
        logger.info("Concurrency changed", extra={"max_workers": max_workers})

    def submit(self, run_id: int) -> None:
        self._pool.submit(self._guarded, run_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _guarded(self, run_id: int) -> None:
        try:
            self.run_now(run_id)
        except Exception:  # a worker thread must never die silently
            logger.exception("Planning run failed unexpectedly", extra={"run_id": run_id})

    def run_now(self, run_id: int) -> None:
        """Execute one planning run to completion. Its own session, own commit.

        Publishes to the same broker a live run uses. It has less to say, since
        planning produces one result rather than a stream of lines, but the
        watcher needs the end of it: without a `done` the run detail page shows
        a live pane that never updates and never reloads, which is what a dry
        run looked like until this was added.
        """
        session = self._session_factory()
        try:
            run = session.get(JobRun, run_id)
            if run is None:
                logger.warning("Run vanished before it started", extra={"run_id": run_id})
                return
            job = session.get(Job, run.job_id)
            if job is None:
                _finish_failed(session, run, "The job was deleted before the run started.")
                return

            run.status = RunStatus.running
            run.started_at = utcnow()
            session.commit()
            _emit(run_id, "status", "Comparing both endpoints. Nothing is being changed.")

            try:
                plan = engine_for(job).plan(job, box=self._box, settings=self._settings)
            except EngineError as exc:
                _finish_failed(session, run, str(exc))
                return

            _persist_plan(session, run, job, plan)
            _emit(
                run_id,
                "done",
                f"{plan.new_count} new, {plan.updated_count} updated, "
                f"{plan.deleted_count} to remove.",
            )
        finally:
            # Always, on every path out. A watcher that is never told the run
            # ended holds its connection open, and a browser only allows about
            # six per host before it stops talking to the application at all.
            broker.finish(run_id)
            session.close()


def _emit(run_id: int, kind: str, text: str) -> None:
    broker.publish(run_id, RunEvent(kind=kind, text=text))


def _finish_failed(session: Session, run: JobRun, message: str) -> None:
    run.status = RunStatus.failed
    run.finished_at = utcnow()
    run.errors_count = 1
    run.summary = {"error": message}
    session.commit()
    _emit(run.id, "done", f"Failed: {message}")
    logger.warning("Run failed", extra={"run_id": run.id, "reason": message})


def _persist_plan(session: Session, run: JobRun, job: Job, plan: Plan) -> None:
    side = side_for(job)

    for change in plan.changes[:MAX_PERSISTED_CHANGES]:
        session.add(
            JobRunChange(
                run_id=run.id,
                action=change.action,
                # A bidirectional plan sets this per change, because bisync
                # changes both sides in one run. One way plans leave it unset
                # and every change lands on the side the job writes to.
                side=change.side or side,
                path=change.path,
                size=change.size,
                message=change.message,
            )
        )

    truncated_rows = max(0, len(plan.changes) - MAX_PERSISTED_CHANGES)

    run.status = RunStatus.success
    run.finished_at = utcnow()
    run.exit_code = 0
    run.files_transferred = plan.new_count + plan.updated_count
    run.files_deleted = plan.deleted_count
    run.bytes_transferred = plan.bytes_to_transfer
    run.errors_count = plan.error_count
    run.command_redacted = "\n".join(plan.commands)
    run.summary = {
        "new": plan.new_count,
        "updated": plan.updated_count,
        "deleted": plan.deleted_count,
        "unchanged": plan.unchanged_count,
        "errors": plan.errors,
        "warnings": plan.warnings,
        "bytes": plan.bytes_to_transfer,
        "dest_file_count": plan.dest_file_count,
        "max_delete_threshold": _threshold(job, plan),
        # The brake means different things in the two directions, so the page
        # cannot word it from the numbers alone.
        "bidirectional": job.direction == Direction.bidirectional,
        "truncated": plan.truncated,
        "rows_omitted": truncated_rows,
    }
    session.commit()
    logger.info(
        "Plan complete",
        extra={
            "run_id": run.id,
            "job": job.name,
            "new": plan.new_count,
            "updated": plan.updated_count,
            "deleted": plan.deleted_count,
        },
    )


def _threshold(job: Job, plan: Plan) -> int:
    """The number the delete brake will actually enforce.

    For bisync `--max-delete` is a **percentage**, not a count, so converting it
    the way a one way job does would report a threshold that has nothing to do
    with what rclone enforces. The percentage is returned unchanged and the run
    page words it differently.
    """
    if job.direction == Direction.bidirectional:
        return job.max_delete_pct

    from app.engines.rclone import resolve_max_delete

    return resolve_max_delete(job.max_delete_pct, plan.dest_file_count)


def latest_runs(session: Session, job_id: int, limit: int = 20) -> list[JobRun]:
    return list(
        session.scalars(
            select(JobRun).where(JobRun.job_id == job_id).order_by(JobRun.id.desc()).limit(limit)
        )
    )


def wait_for(
    session_factory: sessionmaker[Session],
    run_id: int,
    *,
    timeout_seconds: float = 60.0,
    sleep: Callable[[float], None] | None = None,
) -> RunStatus:
    """Block until a run leaves the active states. For tests and the CLI."""
    import time

    sleeper = sleep or time.sleep
    deadline = time.monotonic() + timeout_seconds
    terminal = {RunStatus.success, RunStatus.failed, RunStatus.cancelled, RunStatus.skipped}
    while time.monotonic() < deadline:
        session = session_factory()
        try:
            run = session.get(JobRun, run_id)
            if run is not None and run.status in terminal:
                return run.status
        finally:
            session.close()
        sleeper(0.1)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout_seconds} seconds.")


def started_at_or_none(run: JobRun) -> datetime | None:
    return run.started_at
