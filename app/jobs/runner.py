"""Live run supervision.

The delete brake is two mechanisms, and both are needed:

1. A **pre-flight veto**. The run plans first and refuses before rclone is
   invoked if the plan would delete more than the brake allows. This is what
   makes SPEC section 18's "a job whose source is emptied is refused by the
   delete brake" true, because refusal has to happen before anything is removed.

2. An **in-flight backstop**, `--max-delete`. Verified against rclone 1.74.4:
   the flag aborts partway through, after deleting up to the threshold. On its
   own it is not a refusal, it is a limit on the damage. It is still always
   passed, so a tree that changed between planning and executing cannot run away.

The pre-flight also re-checks the sentinel file. A mount that was healthy at the
last connection test is exactly the thing that fails silently later, and SPEC
section 6.4 describes that failure as the reason the check exists.

Cancellation is SIGTERM, ten seconds, then SIGKILL, per SPEC section 6.3.
Verified: on SIGTERM rclone removes the partial file it was writing, so a
cancelled transfer leaves nothing half-written under a final name. A SIGKILL
skips that cleanup, which is why the grace period is real rather than polite.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.crypto import SecretBox
from app.engines import parsers, process, rcloneconf
from app.engines.base import EngineError, Plan
from app.engines.rclone import (
    RcloneEngine,
    build_sync_command,
    endpoints_and_paths,
    resolve_max_delete,
    side_for,
)
from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE, RemoteConfigError
from app.jobs.events import RunEvent, broker
from app.models import (
    ChangeAction,
    ConnectionType,
    Job,
    JobRun,
    JobRunChange,
    RunStatus,
    utcnow,
)

logger = logging.getLogger(__name__)

MAX_PERSISTED_CHANGES = 5000


class BrakeEngaged(Exception):
    """The pre-flight veto refused the run. Carries a user facing explanation."""


class LiveRunner:
    """Runs live syncs on a worker pool, and can cancel them."""

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
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hivesync-live")
        self._lock = threading.Lock()
        self._active: dict[int, process.StreamingProcess] = {}

    def submit(self, run_id: int) -> None:
        self._pool.submit(self._guarded, run_id)

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active.values())
        for running in active:
            running.terminate()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def cancel(self, run_id: int) -> bool:
        """Ask a running sync to stop. Returns False if it was not running."""
        with self._lock:
            running = self._active.get(run_id)
        if running is None:
            return False
        logger.info("Cancelling run", extra={"run_id": run_id, "pid": running.pid})
        running.terminate()
        return True

    def _guarded(self, run_id: int) -> None:
        try:
            self.run_now(run_id)
        except Exception:  # a worker thread must never die silently
            logger.exception("Live run failed unexpectedly", extra={"run_id": run_id})
            broker.finish(run_id)

    def run_now(self, run_id: int) -> None:
        session = self._session_factory()
        try:
            run = session.get(JobRun, run_id)
            if run is None:
                return
            job = session.get(Job, run.job_id)
            if job is None:
                _fail(session, run, "The job was deleted before the run started.")
                return

            run.status = RunStatus.running
            run.started_at = utcnow()
            session.commit()
            _emit(run_id, "status", "Planning before making any changes.")

            try:
                self._execute(session, run, job)
            except BrakeEngaged as exc:
                _fail(session, run, str(exc), status=RunStatus.failed)
                _emit(run_id, "status", f"Refused: {exc}")
            except (EngineError, RemoteConfigError) as exc:
                _fail(session, run, str(exc))
                _emit(run_id, "status", f"Failed: {exc}")
        finally:
            with self._lock:
                self._active.pop(run_id, None)
            broker.finish(run_id)
            session.close()

    # ----------------------------------------------------------------------

    def _execute(self, session: Session, run: JobRun, job: Job) -> None:
        # Always plan immediately before. A dry run from an hour ago cannot
        # notice a source that failed to mount five minutes ago, and that is
        # precisely the case the brake exists for.
        plan = RcloneEngine().plan(job, box=self._box, settings=self._settings)
        threshold = resolve_max_delete(job.max_delete_pct, plan.dest_file_count)

        self._preflight(job, plan, threshold)

        source, dest, read_path, write_path = endpoints_and_paths(job)
        with rcloneconf.prepare(
            {ALIAS_SOURCE: source, ALIAS_DEST: dest}, box=self._box, settings=self._settings
        ) as prepared:
            argv = build_sync_command(
                job,
                prepared,
                prepared.endpoints[ALIAS_SOURCE].spec(read_path or None),
                prepared.endpoints[ALIAS_DEST].spec(write_path or None),
                max_delete=threshold,
            )
            running = process.stream(
                argv, env=prepared.env, redactor=prepared.redactor, log_label="sync"
            )
            with self._lock:
                self._active[run.id] = running

            run.command_redacted = running.command_line
            session.commit()
            _emit(run.id, "status", "Syncing.")

            log_path = _log_path(self._settings, job, run)
            observed = _consume(running, run.id, log_path)
            exit_code = running.wait()

        _record(session, run, job, plan, observed, exit_code, threshold, log_path)

    def _preflight(self, job: Job, plan: Plan, threshold: int) -> None:
        """Refuse before anything is written. The heart of the criterion."""
        source, _dest, _read, _write = endpoints_and_paths(job)

        # SPEC 6.4: a stale cifs or NFS mount presents as an empty directory,
        # which looks exactly like "delete everything". Re-checked here rather
        # than trusted from the last connection test.
        sentinel = (source.sentinel_file or "").strip()
        if sentinel and source.type == ConnectionType.local:
            root = Path(source.base_path or "/")
            if not (root / sentinel).exists():
                raise BrakeEngaged(
                    f"The sentinel file '{sentinel}' is missing from "
                    f"{source.name}. The source is probably not mounted. Nothing "
                    "was changed."
                )

        if plan.deleted_count > threshold:
            raise BrakeEngaged(
                f"This run would delete {plan.deleted_count} files, and the "
                f"{job.max_delete_pct}% delete brake allows {threshold} out of the "
                f"{plan.dest_file_count} on the destination. Nothing was changed. "
                "Check that the source is complete and mounted. If the deletions "
                "are intended, raise the brake for this job."
            )


def _consume(running: process.StreamingProcess, run_id: int, log_path: Path) -> parsers.DryRunLog:
    """Read the sync's output as it arrives: to the log file, to watchers, and
    into counts.

    Parsing as it streams rather than at the end is what makes a cancelled run
    able to report what it actually did.
    """
    observed = parsers.DryRunLog()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as handle:
        for line in running.lines():
            handle.write(line + "\n")
            _absorb(observed, line)
            _emit(run_id, "line", line)
    return observed


def _absorb(observed: parsers.DryRunLog, line: str) -> None:
    """Fold one live JSON log line into the running totals."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return
    try:
        payload = json.loads(stripped)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return

    obj = payload.get("object")
    message = str(payload.get("msg", ""))

    # A live run reports completed work rather than the dry run's "skipped".
    if isinstance(obj, str) and obj:
        size = payload.get("size")
        size_value = size if isinstance(size, int) and size >= 0 else None
        if message.startswith("Copied") or message.startswith("Updated"):
            observed.operations.append(
                parsers.PlannedOperation(path=obj, operation=parsers.SKIPPED_COPY, size=size_value)
            )
        elif message.startswith("Deleted"):
            observed.operations.append(
                parsers.PlannedOperation(
                    path=obj, operation=parsers.SKIPPED_DELETE, size=size_value
                )
            )

    if payload.get("level") == "error":
        if "max-delete threshold reached" in message:
            observed.max_delete_hit = True
        detail = f"{obj}: {message}" if isinstance(obj, str) and obj else message
        observed.errors.append(detail.strip())


def _record(
    session: Session,
    run: JobRun,
    job: Job,
    plan: Plan,
    observed: parsers.DryRunLog,
    exit_code: int,
    threshold: int,
    log_path: Path,
) -> None:
    """Store what actually happened, including for a cancelled run.

    A cancelled sync reports the work it completed before stopping, not nothing.
    The next run's brake reads the resulting state, so pretending a partial run
    did nothing would mislead the thing that protects the destination.
    """
    side = side_for(job)
    copied = observed.copies
    deleted = observed.deletes

    planned_paths = {change.path for change in plan.changes}
    for op in (copied + deleted)[:MAX_PERSISTED_CHANGES]:
        action = (
            ChangeAction.deleted
            if op.operation == parsers.SKIPPED_DELETE
            else (ChangeAction.new if op.path not in planned_paths else ChangeAction.updated)
        )
        session.add(
            JobRunChange(run_id=run.id, action=action, side=side, path=op.path, size=op.size)
        )

    cancelled = run.status == RunStatus.cancelled or exit_code in (-15, 143, -9, 137)
    if cancelled:
        status = RunStatus.cancelled
    elif exit_code == 0:
        status = RunStatus.success
    else:
        status = RunStatus.failed

    applied_as_planned = (
        status == RunStatus.success
        and len(copied) == plan.new_count + plan.updated_count
        and len(deleted) == plan.deleted_count
    )

    run.status = status
    run.finished_at = utcnow()
    run.exit_code = exit_code
    run.files_transferred = len(copied)
    run.files_deleted = len(deleted)
    run.bytes_transferred = sum(op.size or 0 for op in copied)
    run.errors_count = len(observed.errors)
    run.log_path = str(log_path)
    run.summary = {
        "planned_new": plan.new_count,
        "planned_updated": plan.updated_count,
        "planned_deleted": plan.deleted_count,
        "transferred": len(copied),
        "deleted": len(deleted),
        "errors": parsers.summarise_errors(observed.errors),
        "warnings": plan.warnings,
        "dest_file_count": plan.dest_file_count,
        "max_delete_threshold": threshold,
        "applied_as_planned": applied_as_planned,
        "max_delete_hit": observed.max_delete_hit,
        "cancelled_partway": cancelled and bool(copied or deleted),
    }
    session.commit()
    _emit(
        run.id,
        "done",
        f"{status.value}: {len(copied)} transferred, {len(deleted)} deleted.",
    )
    logger.info(
        "Live run finished",
        extra={
            "run_id": run.id,
            "job": job.name,
            "status": status.value,
            "transferred": len(copied),
            "deleted": len(deleted),
        },
    )


def _fail(
    session: Session, run: JobRun, message: str, *, status: RunStatus = RunStatus.failed
) -> None:
    run.status = status
    run.finished_at = utcnow()
    run.errors_count = 1
    run.summary = {"error": message}
    session.commit()
    logger.warning("Live run failed", extra={"run_id": run.id, "reason": message})


def _log_path(settings: Settings, job: Job, run: JobRun) -> Path:
    """SPEC section 16: per-run logs at /config/logs/<job-id>/<run-id>.log."""
    return settings.log_dir / str(job.id) / f"{run.id}.log"


def _emit(run_id: int, kind: str, text: str) -> None:
    broker.publish(run_id, RunEvent(kind=kind, text=text))
