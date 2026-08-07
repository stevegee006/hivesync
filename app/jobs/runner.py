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

from app import notify
from app import preferences as preferences_store
from app.config import Settings
from app.crypto import SecretBox
from app.engines import bisync, parsers, process, rcloneconf
from app.engines.base import EngineError, Plan
from app.engines.rclone import (
    RcloneEngine,
    build_sync_command,
    endpoints_and_paths,
    resolve_max_delete,
    side_for,
)
from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE, RemoteConfigError
from app.jobs import archive
from app.jobs.events import RunEvent, activity, broker
from app.models import (
    ChangeAction,
    ConnectionType,
    DeleteMode,
    Direction,
    Job,
    JobRun,
    JobRunChange,
    RunStatus,
    RunTrigger,
    utcnow,
)

logger = logging.getLogger(__name__)

MAX_PERSISTED_CHANGES = 5000
PLAN_TIMEOUT_SECONDS = 15 * 60


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
            except (EngineError, RemoteConfigError, archive.ArchiveError) as exc:
                _fail(session, run, str(exc))
                _emit(run_id, "status", f"Failed: {exc}")

            # After the outcome is committed, never inside it: a slow endpoint
            # must not hold a write lock, and a failed notification must not
            # change what the run recorded. SPEC section 16.
            _notify(session, run, job)
            _prune_quiet_cycle(session, run, job)
        finally:
            with self._lock:
                self._active.pop(run_id, None)
            activity.forget(run_id)
            broker.finish(run_id)
            session.close()

    # ----------------------------------------------------------------------

    def _execute(self, session: Session, run: JobRun, job: Job) -> None:
        if job.direction == Direction.bidirectional:
            self._execute_bisync(session, run, job)
            return
        self._execute_one_way(session, run, job)

    # ------------------------------------------------------------ bidirectional

    def _execute_bisync(self, session: Session, run: JobRun, job: Job) -> None:
        """A bidirectional run. SPEC section 10.

        Resync is never automatic. Invariant: `bisync never auto-resyncs`. A job
        that has not been initialised, or whose workdir has been lost, is refused
        with a message that offers the explicit action instead.
        """
        if not job.bisync_initialized and not run.is_resync:
            raise BrakeEngaged(
                f"'{job.name}' has not had its first sync yet. Bidirectional sync "
                "needs one, and it is not automatic: a resync makes the second "
                "path match the first for any file that differs, so it has to be "
                "a decision rather than a side effect. Use First Sync on the job."
            )

        workdir = bisync.workdir_for(str(self._settings.bisync_dir), job.id)
        Path(workdir).mkdir(parents=True, exist_ok=True)
        source, dest, read_path, write_path = endpoints_and_paths(job)

        with rcloneconf.prepare(
            {ALIAS_SOURCE: source, ALIAS_DEST: dest}, box=self._box, settings=self._settings
        ) as prepared:
            path1 = prepared.endpoints[ALIAS_SOURCE].spec(read_path or None)
            path2 = prepared.endpoints[ALIAS_DEST].spec(write_path or None)

            # Pre-flight, the same shape as a one way run. bisync also has its
            # own percentage brake that aborts before changing anything, so this
            # is belt and braces, but it produces the better message.
            if not run.is_resync:
                self._bisync_preflight(job, prepared, path1, path2, workdir)

            archive_flags = []
            if job.delete_mode == DeleteMode.archive:
                # SPEC 7.2: each side archives locally. Never across remotes.
                archive_flags = archive.bisync_args(
                    archive.plan_for(job, path1), archive.plan_for(job, path2)
                )

            argv = bisync.build_bisync_command(
                job,
                prepared,
                path1,
                path2,
                workdir=workdir,
                resync=run.is_resync,
                unattended=run.trigger == RunTrigger.schedule,
                archive=archive_flags,
            )
            running = process.stream(
                argv, env=prepared.env, redactor=prepared.redactor, log_label="bisync"
            )
            with self._lock:
                self._active[run.id] = running

            run.command_redacted = running.command_line
            session.commit()
            _emit(run.id, "status", "Resyncing." if run.is_resync else "Syncing both ways.")

            log_path = _log_path(self._settings, job, run)
            observed, text = _consume_bisync(running, run.id, log_path)
            exit_code = running.wait()

        if bisync.needs_resync(text):
            # The workdir can be lost independently of what the database says, so
            # rclone's own message is the source of truth.
            job.bisync_initialized = False
            session.commit()
            _fail(
                session,
                run,
                f"'{job.name}' needs a first sync before it can run again. Its "
                "listing state is missing, which happens on a first run or if the "
                "working directory was lost. Nothing was changed. Use First Sync "
                "to rebuild it.",
            )
            _emit(run.id, "status", "Needs a first sync.")
            return

        deltas = bisync.parse_deltas(text)
        if deltas.safety_abort:
            _fail(
                session,
                run,
                f"rclone refused this run: {deltas.safety_abort} Nothing was "
                "changed. Check that both endpoints are fully mounted. If the "
                "deletions are intended, raise the delete brake for this job.",
            )
            return

        if run.is_resync and exit_code == 0:
            job.bisync_initialized = True
            session.commit()

        _record_bisync(session, run, job, deltas, observed, exit_code, log_path)

    def _bisync_preflight(
        self, job: Job, prepared: rcloneconf.Prepared, path1: str, path2: str, workdir: str
    ) -> None:
        """Refuse before anything is written, using a bisync dry run."""
        argv = bisync.build_bisync_command(
            job, prepared, path1, path2, workdir=workdir, dry_run=True
        )
        result = process.run(
            argv,
            env=prepared.env,
            redactor=prepared.redactor,
            timeout_seconds=PLAN_TIMEOUT_SECONDS,
            log_label="bisync --dry-run",
        )
        text = result.stdout + "\n" + result.stderr
        if bisync.needs_resync(text):
            # Handled by the caller after the real attempt; nothing to veto here.
            return
        deltas = bisync.parse_deltas(text)
        if deltas.safety_abort:
            raise BrakeEngaged(
                f"rclone refused this run before making any change: "
                f"{deltas.safety_abort} Check that both endpoints are fully "
                "mounted. If the deletions are intended, raise the delete brake."
            )

    # ---------------------------------------------------------------- one way

    def _execute_one_way(self, session: Session, run: JobRun, job: Job) -> None:
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
            destination = prepared.endpoints[ALIAS_DEST].spec(write_path or None)
            archive_flags: list[str] = []
            if job.delete_mode == DeleteMode.archive:
                # Resolved per run: the timestamped layout puts the run stamp in
                # the path, so it cannot be computed once at save time.
                plan_archive = archive.plan_for(job, destination)
                archive_flags = archive.sync_args(plan_archive)
                _emit(
                    run.id,
                    "status",
                    f"Deletions will be archived to {plan_archive.backup_dir}.",
                )

            argv = build_sync_command(
                job,
                prepared,
                prepared.endpoints[ALIAS_SOURCE].spec(read_path or None),
                destination,
                max_delete=threshold,
                archive=archive_flags,
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
            if _absorb(observed, line, run_id):
                # A stats record. It drives the progress panel and the activity
                # strip, and in the live pane it is a wall of raw JSON that
                # buries the lines someone is actually reading. The log file on
                # disk still gets it.
                continue
            _emit(run_id, "line", line)
    return observed


def _absorb(observed: parsers.DryRunLog, line: str, run_id: int) -> bool:
    """Fold one live JSON log line into the running totals.

    Returns True when the line was a stats record, which the caller keeps out
    of the live log pane: it is a progress report, not something to read.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        payload = json.loads(stripped)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False

    # A periodic progress report rather than a per-file event. rclone emits one
    # on the --stats interval. It drives the progress panel and the activity
    # strip, and is kept out of both the log backlog and the live pane, which
    # exist for the log a person reads.
    stats = payload.get("stats")
    if isinstance(stats, dict):
        parsed = parsers.parse_stats(stats)
        observed.stats = stats
        activity.record(run_id, parsed)
        broker.publish(
            run_id,
            RunEvent(
                kind="stats",
                text="",
                data={
                    "bytes": parsed.bytes_done,
                    "total_bytes": parsed.total_bytes,
                    "speed": parsed.speed,
                    "eta": parsed.eta_seconds,
                    "percentage": parsed.percentage,
                    "transfers": parsed.transfers,
                    "total_transfers": parsed.total_transfers,
                    # The files actually in flight, at most `--transfers` of
                    # them. Verified against 1.74.4: a per-file `eta` is null
                    # until rclone has enough history to estimate one, so the
                    # run page has to render a row that has no ETA yet rather
                    # than waiting for one.
                    "files": [
                        {
                            "name": progress.name,
                            "bytes": progress.bytes_done,
                            "size": progress.size,
                            "percentage": progress.percentage,
                            "speed": progress.speed,
                            "eta": progress.eta_seconds,
                        }
                        for progress in parsed.transferring
                    ],
                },
            ),
        )
        return True

    obj = payload.get("object")
    message = str(payload.get("msg", ""))

    # A live run reports completed work rather than the dry run's "skipped".
    if isinstance(obj, str) and obj:
        size = payload.get("size")
        size_value = size if isinstance(size, int) and size >= 0 else None
        # "Copied" as a substring, not a prefix. Verified against rclone 1.74.4,
        # a file large enough for the multi-thread path logs "Multi-thread
        # Copied (new)", so matching the start of the message silently missed
        # every large transfer: a 3 GB file recorded as zero files and zero
        # bytes, and the lifetime total on the dashboard was wrong by that much.
        #
        # "Updated modification time in destination" is deliberately not a
        # transfer. It was matched before by startswith("Updated"), which
        # counted a modtime-only touch as a transferred file.
        if "Copied" in message:
            # Verified against 1.74.4: "Copied (new)" or "Copied (replaced
            # existing)", and "Multi-thread Copied (new)" for a file over the
            # cutoff. This is the only place the two can be told apart, so a
            # live run that does not record it here reports every transfer as
            # neither new nor updated.
            observed.operations.append(
                parsers.PlannedOperation(
                    path=obj,
                    operation=parsers.SKIPPED_COPY,
                    size=size_value,
                    replaced="(new)" not in message,
                )
            )
        elif message.startswith("Deleted"):
            observed.operations.append(
                parsers.PlannedOperation(
                    path=obj, operation=parsers.SKIPPED_DELETE, size=size_value
                )
            )
        elif message.startswith("Moved into backup dir"):
            # An archiving run emits no "Deleted" line at all. It emits this,
            # preceded by a "Moved (server-side)" line for the same object, so
            # only this one is counted or the file would be counted twice.
            observed.operations.append(
                parsers.PlannedOperation(
                    path=obj, operation=parsers.SKIPPED_ARCHIVE, size=size_value
                )
            )

    if payload.get("level") == "error":
        if "max-delete threshold reached" in message:
            observed.max_delete_hit = True
        detail = f"{obj}: {message}" if isinstance(obj, str) and obj else message
        observed.errors.append(detail.strip())

    return False


def _bytes_transferred(observed: parsers.DryRunLog, copied: list[parsers.PlannedOperation]) -> int:
    """Bytes moved, preferring rclone's own total over our reconstruction."""
    if isinstance(observed.stats, dict):
        reported = observed.stats.get("bytes")
        if isinstance(reported, int) and reported >= 0:
            return reported
    return sum(op.size or 0 for op in copied)


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
    archived = observed.archived
    # An archived file left the destination just as surely as a deleted one, so
    # it counts here. The plan calls it a deletion too: planning never passes
    # --backup-dir, because where a file goes does not change whether it goes.
    deleted = observed.removals

    planned_paths = {change.path for change in plan.changes}
    actions = {
        parsers.SKIPPED_DELETE: ChangeAction.deleted,
        parsers.SKIPPED_ARCHIVE: ChangeAction.archived,
    }
    for op in (copied + deleted)[:MAX_PERSISTED_CHANGES]:
        action = actions.get(
            op.operation,
            ChangeAction.new if op.path not in planned_paths else ChangeAction.updated,
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
    run.files_archived = len(archived)
    # rclone's own accounting where we have it. Summing per-file sizes counts a
    # whole file even when only part of it moved, and depends on every message
    # being recognised, which is exactly what went wrong with multi-thread
    # copies. The stats block is the figure rclone itself reports.
    run.bytes_transferred = _bytes_transferred(observed, copied)
    run.errors_count = len(observed.errors)
    run.log_path = str(log_path)
    run.summary = {
        # The same keys a dry run writes, so one screen can read either. Without
        # these the run page fell back to zero for New, Updated and Unchanged on
        # every live run, including ones that copied thousands of files, because
        # it was reading dry run keys that a live run never wrote.
        "new": len(observed.created),
        "updated": len(observed.replacements),
        # From the pre-flight plan. A live run never sees the files it did not
        # touch, so this is the only source for it, and it is the same number
        # the dry run of the same job reports.
        "unchanged": plan.unchanged_count,
        "bytes": run.bytes_transferred,
        "planned_new": plan.new_count,
        "planned_updated": plan.updated_count,
        "planned_deleted": plan.deleted_count,
        "transferred": len(copied),
        "deleted": len(deleted),
        "archived": len(archived),
        "errors": parsers.summarise_errors(observed.errors),
        "warnings": plan.warnings,
        "dest_file_count": plan.dest_file_count,
        "max_delete_threshold": threshold,
        "applied_as_planned": applied_as_planned,
        "max_delete_hit": observed.max_delete_hit,
        "cancelled_partway": cancelled and bool(copied or deleted),
    }
    session.commit()
    removed = f"{len(archived)} archived" if archived else f"{len(deleted)} deleted"
    _emit(run.id, "done", f"{status.value}: {len(copied)} transferred, {removed}.")
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


def _consume_bisync(
    running: process.StreamingProcess, run_id: int, log_path: Path
) -> tuple[parsers.DryRunLog, str]:
    """Read bisync output as it arrives, keeping the full text for delta parsing.

    bisync reports its per-side summary in prose rather than the structured
    `skipped` field a sync dry run uses, so the whole stream is retained.
    """
    observed = parsers.DryRunLog()
    lines: list[str] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as handle:
        for line in running.lines():
            handle.write(line + "\n")
            lines.append(line)
            if _absorb(observed, line, run_id):
                continue  # See the note in the one way reader.
            _emit(run_id, "line", line)
    return observed, "\n".join(lines)


def _record_bisync(
    session: Session,
    run: JobRun,
    job: Job,
    deltas: bisync.BisyncDeltas,
    observed: parsers.DryRunLog,
    exit_code: int,
    log_path: Path,
) -> None:
    """Store the outcome of a bidirectional run, per side."""
    cancelled = run.status == RunStatus.cancelled or exit_code in (-15, 143, -9, 137)
    if cancelled:
        status = RunStatus.cancelled
    elif exit_code == 0:
        status = RunStatus.success
    else:
        status = RunStatus.failed

    run.status = status
    run.finished_at = utcnow()
    run.exit_code = exit_code
    # bisync archives through the same machinery, emitting "Moved into backup
    # dir" and no "Deleted" line, so removals rather than deletes.
    removed = observed.removals
    run.files_transferred = len(observed.copies)
    run.files_deleted = len(removed)
    run.files_archived = len(observed.archived)
    run.bytes_transferred = sum(op.size or 0 for op in observed.copies)
    run.errors_count = len(observed.errors)
    run.log_path = str(log_path)
    run.summary = {
        "bidirectional": True,
        "resync": run.is_resync,
        "path1": {
            "new": deltas.path1_new,
            "modified": deltas.path1_modified,
            "deleted": deltas.path1_deleted,
        },
        "path2": {
            "new": deltas.path2_new,
            "modified": deltas.path2_modified,
            "deleted": deltas.path2_deleted,
        },
        "transferred": len(observed.copies),
        "deleted": len(removed),
        "archived": len(observed.archived),
        "errors": parsers.summarise_errors(observed.errors),
        # A percentage for bisync, not a count. See app/engines/bisync.py.
        "max_delete_pct": job.max_delete_pct,
        "cancelled_partway": cancelled and bool(observed.copies or removed),
    }
    session.commit()
    _emit(run.id, "done", f"{status.value}: {deltas.total_changes} changes reconciled.")
    logger.info(
        "Bidirectional run finished",
        extra={
            "run_id": run.id,
            "job": job.name,
            "status": status.value,
            "resync": run.is_resync,
        },
    )


def _notify(session: Session, run: JobRun, job: Job) -> None:
    """Tell whoever asked to be told. Never lets a failure here reach the run."""
    try:
        if not notify.should_notify(job, run.status):
            return
        preferences = preferences_store.load(session)
        if preferences.notify_target == "none":
            return
        payload = notify.build_payload(job, run, base_url=preferences.base_url)
        notify.send(preferences, payload)
    except Exception:
        logger.exception("Could not dispatch notification", extra={"run_id": run.id})


def _prune_quiet_cycle(session: Session, run: JobRun, job: Job) -> None:
    """Drop a continuous cycle that succeeded and moved nothing.

    A sixty second loop is 1,440 runs a day. Keeping the ones that did nothing
    buries the ones that did, and the run history exists to answer "what
    happened to my files". A failure is always kept, including a refusal by the
    delete brake: that is exactly the run someone goes looking for.

    `last_checked_at` carries the proof of life instead, so the UI can still
    distinguish "watching quietly" from "stopped".
    """
    if not job.continuous or run.status != RunStatus.success:
        return
    if run.files_transferred or run.files_deleted or run.errors_count:
        return
    session.delete(run)
    job.last_checked_at = utcnow()
    session.commit()


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
