"""The rclone engine.

`plan()` runs two phases, per SPEC section 8 but with the roles corrected after
reading the real behaviour of rclone 1.74.4:

Phase 1, presence. `check --size-only` with the named per-category outputs. Used
only to learn which paths exist on which side. Presence is independent of hashes
and modification times, so it is reliable for every backend pairing. `--size-only`
also means no hashing, which on a NAS is the difference between a usable dry run
and one nobody waits for.

Phase 2, authority. `sync --dry-run --use-json-log` with the job's real flags.
This is what decides whether a file would actually be copied, because it is the
same code path the live run takes.

The spec has phase 1 classifying and phase 2 confirming. That is backwards:
`check` compares hash and size and never modification times, so for the default
mtime-based comparison it cannot reproduce what `sync` would do. Presence from
phase 1 plus intent from phase 2 is the combination that is actually sound.
"""

from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path

from app import capabilities
from app.config import Settings
from app.crypto import SecretBox
from app.engines import parsers, process, rcloneconf
from app.engines.base import EngineError, Plan, PlannedChange, SyncEngine
from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE, RemoteConfigError
from app.models import (
    ChangeAction,
    ChangeSide,
    CompareMode,
    Connection,
    DeleteMode,
    Direction,
    Job,
)

logger = logging.getLogger(__name__)

PLAN_TIMEOUT_SECONDS = 15 * 60

# The planning pass needs to enumerate every deletion, so it must not stop at the
# job's brake. rclone aborts at --max-delete even during a dry run (exit 7,
# "max-delete threshold reached"), truncating the plan at exactly the number the
# warning is about. The brake is evaluated afterwards against the full count.
# Invariant 7 is untouched: a live sync always passes the job's real value.
_PLANNING_MAX_DELETE = 1_000_000_000


class RcloneEngine(SyncEngine):
    name = "rclone"

    def plan(self, job: Job, *, box: SecretBox, settings: Settings) -> Plan:
        if job.direction == Direction.bidirectional:
            # A guard, not a limitation: bidirectional jobs are planned by
            # BisyncEngine, which the planner selects. Reaching here means
            # something called the wrong engine directly.
            raise EngineError(
                "RcloneEngine plans one way jobs only. Bidirectional jobs are "
                "planned by BisyncEngine."
            )

        source, dest = _endpoints_for(job)
        # The subpaths swap with the connections. Reading from dest_connection
        # while still applying source_path would plan against the wrong tree.
        read_path, write_path = _paths_for(job)
        connections = {ALIAS_SOURCE: source, ALIAS_DEST: dest}

        try:
            with rcloneconf.prepare(connections, box=box, settings=settings) as prepared:
                src_spec = prepared.endpoints[ALIAS_SOURCE].spec(read_path or None)
                dst_spec = prepared.endpoints[ALIAS_DEST].spec(write_path or None)
                return _run_phases(job, prepared, src_spec, dst_spec, source, dest)
        except RemoteConfigError as exc:
            raise EngineError(str(exc)) from exc

    def execute(self, job: Job, *, box: SecretBox, settings: Settings) -> Plan:
        """Not used. A live run goes through jobs.runner, which needs to stream.

        Kept to satisfy the interface. The runner calls build_sync_command and
        drives the process itself, because a live run has to be watched and
        cancellable and this signature cannot express that.
        """
        raise EngineError(
            "Live runs are driven by the job runner, not by calling execute directly."
        )


def build_sync_command(
    job: Job,
    prepared: rcloneconf.Prepared,
    src_spec: str,
    dst_spec: str,
    *,
    max_delete: int,
    archive: list[str] | None = None,
) -> list[str]:
    """The argv for a live one way sync.

    --max-delete is always present. Invariant 7 has no exceptions, and this is
    the only place a live sync command is built, so there is no path around it.

    Deletion only happens at all when the job asks for it: without delete_mode
    the command is a copy, which never removes anything from the destination.
    """
    # A copy cannot remove anything, so it is used whenever deletion is off.
    # Archiving is still a sync: rclone moves the extra file aside rather than
    # deleting it, which is a deletion from the destination's point of view.
    operation = "copy" if job.delete_mode == DeleteMode.none else "sync"

    return prepared.argv(
        operation,
        src_spec,
        dst_spec,
        "--use-json-log",
        "-v",
        "--stats",
        "5s",
        "--stats-one-line",
        # The brake. Resolved from the percentage against the destination's real
        # file count, because rclone takes a count and no percentage flag exists.
        "--max-delete",
        str(max_delete),
        *(archive or []),
        *comparison_args(job),
        *filter_args(job),
        *performance_args(job),
        *quiet_period_args(job),
    )


def endpoints_and_paths(job: Job) -> tuple[Connection, Connection, str, str]:
    """Direction-resolved connections and their subpaths, for the runner."""
    source, dest = _endpoints_for(job)
    read_path, write_path = _paths_for(job)
    return source, dest, read_path, write_path


def _endpoints_for(job: Job) -> tuple[Connection, Connection]:
    """Resolve which connection is read and which is written.

    "source" and "dest" are positional labels, equivalent to rclone's path1 and
    path2. Direction alone decides the flow, so a dest_to_source job writes to the
    connection named source.
    """
    if job.direction == Direction.dest_to_source:
        return job.dest_connection, job.source_connection
    return job.source_connection, job.dest_connection


def _paths_for(job: Job) -> tuple[str, str]:
    if job.direction == Direction.dest_to_source:
        return job.dest_path, job.source_path
    return job.source_path, job.dest_path


def filter_args(job: Job) -> list[str]:
    """Filter flags from the job's own rules plus any presets it references.

    Presets first, then the job's own rules, so a job can always override what a
    preset excluded. rclone applies the first matching rule.
    """
    args: list[str] = []
    filters = job.filters or {}

    for rule in filters.get("include", []) or []:
        if str(rule).strip():
            args += ["--include", str(rule).strip()]

    for preset in job.filter_presets:
        for rule in (preset.rules or {}).get("exclude", []) or []:
            if str(rule).strip():
                args += ["--exclude", str(rule).strip()]

    for rule in filters.get("exclude", []) or []:
        if str(rule).strip():
            args += ["--exclude", str(rule).strip()]

    min_size = filters.get("min_size")
    if min_size:
        args += ["--min-size", str(min_size)]
    max_age = filters.get("max_age")
    if max_age:
        args += ["--max-age", str(max_age)]

    return args


def comparison_args(job: Job) -> list[str]:
    """Flags that decide whether two files are considered the same.

    Checksum is only ever requested when the endpoints share a hash type. The job
    editor blocks the combination, and this is the second line of defence: asking
    for --checksum against a hash-less backend makes rclone compare nothing useful.
    """
    args: list[str] = []
    if job.compare_mode == CompareMode.checksum:
        shared = capabilities.for_connection(job.source_connection).hashes & (
            capabilities.for_connection(job.dest_connection).hashes
        )
        if not shared:
            raise EngineError(
                "This job is set to compare by checksum, but its endpoints share "
                "no hash type. Test both connections, then choose comparison by "
                "modification time and size."
            )
        args.append("--checksum")
    elif job.compare_mode == CompareMode.size_only:
        args.append("--size-only")

    if job.modify_window:
        args += ["--modify-window", job.modify_window]
    return args


def performance_args(job: Job) -> list[str]:
    args: list[str] = []
    if job.transfers:
        args += ["--transfers", str(job.transfers)]
    if job.checkers:
        args += ["--checkers", str(job.checkers)]
    if job.bwlimit:
        args += ["--bwlimit", job.bwlimit]
    return args


def quiet_period_args(job: Job) -> list[str]:
    """Leave a file alone until it has stopped changing.

    `--min-age` skips anything modified more recently than the given age, so a
    file still being written is picked up on a later run instead of copied half
    finished. Verified present in rclone 1.74.4.

    **Applied to every run, not only continuous ones.** It was continuous only,
    on the reasoning that skipping recent files would surprise someone who had
    just pressed Run. That is backwards for the case it exists for: a download
    client writing into the source directory does not know or care that a
    schedule fired at 2am, and half a file copied unattended is worse than a
    file collected on the next run.

    Off when set to zero, which is what every job that predates this gets, so no
    existing schedule changed behaviour silently.

    Modification time is the signal, so this catches a file still being appended
    to. It does not catch a client that writes the whole file and then rewrites
    it in place without touching the mtime. For that, exclude the client's
    temporary names: the "Downloads in progress" filter preset does.
    """
    # `or 0` because a Job built in memory has not had the column default
    # applied yet, so the attribute is None rather than 30 until it is flushed.
    seconds = job.quiet_period_seconds or 0
    if seconds <= 0:
        return []
    return ["--min-age", f"{seconds}s"]


def resolve_max_delete(pct: int, dest_file_count: int) -> int:
    """Turn the job's percentage into the count rclone actually takes.

    SPEC section 6.4 and invariant 7 both describe --max-delete as a percentage.
    Verified against rclone 1.74.4: it is an int count, and no percentage flag
    exists. Rounded up, and never below one, so a brake on a small tree still
    permits a single intentional deletion.
    """
    if dest_file_count <= 0:
        return 1
    return max(1, math.ceil(dest_file_count * pct / 100))


def _run_phases(
    job: Job,
    prepared: rcloneconf.Prepared,
    src_spec: str,
    dst_spec: str,
    source_connection: Connection,
    dest_connection: Connection,
) -> Plan:
    # quiet_period_args here as well as in the live command: without it a dry
    # run lists a file the run would skip, and the two disagree about the same
    # tree.
    shared = filter_args(job) + performance_args(job) + quiet_period_args(job)
    plan = Plan()

    with tempfile.TemporaryDirectory(prefix="hivesync-plan-") as workdir:
        work = Path(workdir)
        categories = {
            "missing_on_dest": work / "missing_on_dst",
            "missing_on_source": work / "missing_on_src",
            "differing": work / "differ",
            "matching": work / "match",
            "errored": work / "error",
        }

        # Phase 1: presence only. --size-only avoids hashing entirely, and no
        # comparison mode affects whether a path exists.
        check_argv = prepared.argv(
            "check",
            src_spec,
            dst_spec,
            "--size-only",
            "--missing-on-dst",
            str(categories["missing_on_dest"]),
            "--missing-on-src",
            str(categories["missing_on_source"]),
            "--differ",
            str(categories["differing"]),
            "--match",
            str(categories["matching"]),
            "--error",
            str(categories["errored"]),
            *shared,
        )
        check_result = process.run(
            check_argv,
            env=prepared.env,
            redactor=prepared.redactor,
            timeout_seconds=PLAN_TIMEOUT_SECONDS,
            log_label="check",
        )
        plan.commands.append(check_result.command_line)

        # Exit 1 means differences were found, which is the normal case here and
        # not a failure. Anything else is.
        if not check_result.ok and check_result.exit_code != parsers.CHECK_EXIT_DIFFERENCES:
            raise EngineError(
                f"Could not compare the two endpoints. {check_result.failure_summary()}"
            )

        presence = parsers.parse_presence(**categories)

        # Phase 2: what the real flag set would actually do.
        sync_argv = prepared.argv(
            "sync",
            src_spec,
            dst_spec,
            "--dry-run",
            "--use-json-log",
            "-v",
            "--max-delete",
            str(_PLANNING_MAX_DELETE),
            *comparison_args(job),
            *shared,
        )
        sync_result = process.run(
            sync_argv,
            env=prepared.env,
            redactor=prepared.redactor,
            timeout_seconds=PLAN_TIMEOUT_SECONDS,
            log_label="sync --dry-run",
        )
        plan.commands.append(sync_result.command_line)

    dry = parsers.parse_dry_run(sync_result.stdout + "\n" + sync_result.stderr)

    if not sync_result.ok and not dry.operations and not dry.errors:
        raise EngineError(f"Could not build a plan for this job. {sync_result.failure_summary()}")

    _reconcile(plan, job, presence, dry)
    _add_warnings(plan, job, presence, source_connection, dest_connection)
    return plan


def _reconcile(
    plan: Plan, job: Job, presence: parsers.PresenceReport, dry: parsers.DryRunLog
) -> None:
    """Combine presence with intent.

    new     = would be copied and does not exist on the destination
    updated = would be copied and does exist
    deleted = would be deleted
    unchanged = present on the source but not being copied
    """
    would_copy = {op.path: op for op in dry.copies}
    # Planning never passes --backup-dir, since where a file goes does not change
    # whether it goes. removals rather than deletes anyway, so that a plan built
    # with one would still count what leaves the destination.
    would_delete = {op.path: op for op in dry.removals}

    for path, op in sorted(would_copy.items()):
        is_new = path in presence.missing_on_dest
        plan.changes.append(
            PlannedChange(
                action=ChangeAction.new if is_new else ChangeAction.updated,
                path=path,
                size=op.size,
            )
        )
        plan.bytes_to_transfer += op.size or 0

    for path, op in sorted(would_delete.items()):
        plan.changes.append(PlannedChange(action=ChangeAction.deleted, path=path, size=op.size))

    for path in sorted(presence.errored):
        plan.changes.append(
            PlannedChange(
                action=ChangeAction.error,
                path=path,
                message="This file could not be compared.",
            )
        )

    plan.unchanged_count = len(presence.present_on_source - set(would_copy))
    plan.dest_file_count = presence.dest_file_count
    plan.errors = parsers.summarise_errors(dry.errors)
    plan.truncated = dry.max_delete_hit


def _add_warnings(
    plan: Plan,
    job: Job,
    presence: parsers.PresenceReport,
    source_connection: Connection,
    dest_connection: Connection,
) -> None:
    """Everything the reviewer needs to know that the counts do not say."""
    threshold = resolve_max_delete(job.max_delete_pct, plan.dest_file_count)
    if plan.deleted_count > threshold:
        plan.warnings.append(
            f"This plan deletes {plan.deleted_count} files, which is more than the "
            f"{job.max_delete_pct}% delete brake allows for a destination holding "
            f"{plan.dest_file_count} files, a limit of {threshold}. A live run would "
            "stop partway through. Check that the source is complete and mounted "
            "before allowing this."
        )

    # Comparison blind spot. Verified: with --size-only, a file whose content
    # changed but whose size did not is reported identical.
    source_caps = capabilities.for_connection(source_connection)
    dest_caps = capabilities.for_connection(dest_connection)
    if job.compare_mode == CompareMode.size_only:
        plan.warnings.append(
            "Comparison is by size only, so a file whose contents changed without "
            "changing size will not be detected as different."
        )
    elif not source_caps.probed or not dest_caps.probed:
        # Not the same thing as having no hashes. Saying "these share no hash
        # type" when the truth is that nobody has looked sends the reader off
        # debugging the wrong problem.
        unprobed = [
            connection.name
            for connection, caps in ((source_connection, source_caps), (dest_connection, dest_caps))
            if not caps.probed
        ]
        plan.warnings.append(
            f"{' and '.join(unprobed)} has not been tested, so its capabilities are "
            "unknown and this plan used modification time and size. Test the "
            "connection to find out whether checksum comparison is available."
        )
    elif not (source_caps.hashes & dest_caps.hashes):
        plan.warnings.append(
            "These endpoints share no hash type, so files are compared on "
            "modification time and size. A change that alters neither will not be "
            "detected."
        )

    if plan.truncated:
        plan.warnings.append(
            "rclone stopped planning at the delete threshold, so this list is "
            "incomplete. The real number of deletions is higher than shown."
        )

    if presence.errored:
        plan.warnings.append(
            f"{len(presence.errored)} files could not be compared. They are listed "
            "with the errors below."
        )


def side_for(job: Job) -> ChangeSide:
    """Which side a plan's changes land on, for JobRunChange.side."""
    return ChangeSide.source if job.direction == Direction.dest_to_source else ChangeSide.dest


__all__ = [
    "RcloneEngine",
    "build_sync_command",
    "comparison_args",
    "endpoints_and_paths",
    "filter_args",
    "performance_args",
    "resolve_max_delete",
    "side_for",
]
