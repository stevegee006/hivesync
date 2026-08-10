"""Bidirectional sync.

SPEC section 10 calls this the place naive implementations lose data. Everything
below was verified against rclone 1.74.4 rather than taken from the spec.

**`--max-delete` means something different here.** For `sync` it is a count of
files; for `bisync` it is a **percentage**. Verified:

    --max-delete 10  ->  "Safety abort: too many deletes (>10%, 3 of 10)"
    --max-delete 2   ->  "Safety abort: too many deletes (>2%, 3 of 10)"

So `resolve_max_delete`, which converts the job's percentage into a count for
`sync`, must never be used here. Passing a count of 200 would be read as 200
percent and disable the brake entirely, on the one direction that can damage both
copies. The job's percentage is passed through unchanged.

That also makes bisync's brake **pre-flight**: it aborts before making changes,
where `sync --max-delete` stops partway through.

Other verified behaviour:

- A first run without `--resync`, and a run whose workdir has been wiped, produce
  the *same* error: `Bisync aborted. Must run --resync to recover.`, exit 7. One
  detection path drives one recovery prompt, and it means `bisync_initialized`
  cannot be trusted alone: the workdir can vanish underneath it.
- `--conflict-resolve newer --conflict-loser num` renames the loser to
  `<name>.conflict1` and copies it to the other side, so no version is discarded.
- `--workdir` defaults to `/root/.cache/rclone/bisync`, which is wrong for a
  container running as uid 1000 and is not persistent. Always set it.
- The colour flag is `--color NEVER`. There is no `--no-color`; passing one makes
  rclone misparse the whole command.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.crypto import SecretBox
from app.engines import process, rcloneconf
from app.engines.base import EngineError, Plan, PlannedChange, SyncEngine
from app.engines.rclone import (
    PLAN_TIMEOUT_SECONDS,
    comparison_args,
    endpoints_and_paths,
    filter_args,
    performance_args,
)
from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE, Prepared, RemoteConfigError
from app.models import ChangeAction, ChangeSide, ConflictResolve, Direction, Job

logger = logging.getLogger(__name__)

# Emitted both when a job has never been initialised and when its workdir has
# been lost. Both mean the same thing to an operator: it needs a resync.
RESYNC_REQUIRED_MARKER = "Must run --resync to recover"

# bisync's own pre-flight refusals, which fire before it changes anything. There
# is more than one: "too many deletes" against the --max-delete percentage, and
# "all files were changed", which guards against a tree that has been replaced
# wholesale. Both end with this phrase, in every output format.
#
# Detecting on the phrase rather than on "Safety abort:" is deliberate. Under
# --use-json-log rclone moves that prefix into a separate `object` field, so the
# obvious marker silently never matches.
SAFETY_ABORT_MARKER = "Run with --force if desired"

# "Path1:    2 changes:    0 new,    0 modified,    2 deleted"
_DELTA_RE = re.compile(
    r"(Path1|Path2):\s+\d+ changes:\s+(\d+) new,\s+(\d+) modified,\s+(\d+) deleted"
)

# The per-file diff lines, which is what makes a bidirectional dry run useful
# rather than just a pair of totals.
#
# **The command passes --use-json-log**, so what arrives is not the plain text a
# human sees at a terminal. Captured verbatim from rclone 1.74.4:
#
#   {"time":"...","level":"notice",
#    "msg":"- Path1             File is new              - one.txt",
#    "source":"bisync/deltas.go:259"}
#
# The line to match is inside `msg`. Matching the raw JSON line would capture
# the trailing `","source":"..."}` as part of the filename, which is exactly
# what happened when this was first written against hand-run output.
#
# The path is relative, which is what to show. The "Queue copy to Path2" lines
# that follow carry the same information as an absolute destination path, and
# are deliberately not parsed: two sources for one fact drift apart.
_CHANGE_RE = re.compile(
    r"-\s+(Path1|Path2)\s+(File is new|File was deleted|File changed[^-]*?)\s+-\s+(\S.*?)\s*$"
)

# Which of the two paths a change was seen on. Path1 is the job's source
# connection and Path2 its destination, which is how the command is built.
_SIDE_FOR_PATH = {"Path1": ChangeSide.source, "Path2": ChangeSide.dest}

_ACTION_FOR_DIFF = {
    "File is new": ChangeAction.new,
    "File was deleted": ChangeAction.deleted,
}


def _messages(text: str) -> list[str]:
    """The human readable lines, whichever log format produced them.

    Under `--use-json-log` each line is an object whose `msg` carries the text,
    and a `msg` can itself contain newlines. Anything that is not JSON is passed
    through unchanged, so output captured at a terminal still parses.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("{"):
            lines.append(raw)
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            lines.append(raw)
            continue
        message = payload.get("msg") if isinstance(payload, dict) else None
        if isinstance(message, str):
            lines.extend(message.splitlines())
    return lines


def parse_planned_changes(text: str) -> list[PlannedChange]:
    """Read the per-file differences out of a bisync dry run.

    Tolerant in the same way `parse_deltas` is: an unrecognised line is skipped
    rather than raising, because a wording change should cost detail on a
    preview screen, not block every bidirectional job.
    """
    changes: list[PlannedChange] = []
    for line in _messages(text):
        match = _CHANGE_RE.search(line)
        if match is None:
            continue
        side_name, diff, path = match.group(1), match.group(2).strip(), match.group(3)
        # Anything that is not new or deleted is a modification, and rclone
        # spells out which attributes moved: keep that as the message rather
        # than flattening every variant to "updated".
        action = _ACTION_FOR_DIFF.get(diff, ChangeAction.updated)
        detail = diff if action is ChangeAction.updated else None
        changes.append(
            PlannedChange(
                action=action,
                path=path,
                message=detail,
                side=_SIDE_FOR_PATH[side_name],
            )
        )
    return changes


def workdir_for(settings_bisync_dir: str, job_id: int) -> str:
    """Persistent listing state, per SPEC section 10.2.

    Losing this forces another resync, so it lives on the /config volume rather
    than in rclone's default cache directory.
    """
    return f"{settings_bisync_dir}/{job_id}"


@dataclass(frozen=True)
class BisyncDeltas:
    """What bisync says changed on each side."""

    path1_new: int = 0
    path1_modified: int = 0
    path1_deleted: int = 0
    path2_new: int = 0
    path2_modified: int = 0
    path2_deleted: int = 0
    resync_required: bool = False
    safety_abort: str | None = None

    @property
    def total_deleted(self) -> int:
        return self.path1_deleted + self.path2_deleted

    @property
    def total_changes(self) -> int:
        return (
            self.path1_new
            + self.path1_modified
            + self.path1_deleted
            + self.path2_new
            + self.path2_modified
            + self.path2_deleted
        )


def parse_deltas(text: str) -> BisyncDeltas:
    """Read bisync's per-side change summary out of its output.

    Deliberately tolerant: a format change should degrade to reporting zero
    changes and let the run proceed under rclone's own brake, rather than
    throwing and blocking every bidirectional job.
    """
    values = {"Path1": [0, 0, 0], "Path2": [0, 0, 0]}
    for match in _DELTA_RE.finditer(text):
        side = match.group(1)
        values[side] = [int(match.group(2)), int(match.group(3)), int(match.group(4))]

    abort = _find_safety_abort(text)

    return BisyncDeltas(
        path1_new=values["Path1"][0],
        path1_modified=values["Path1"][1],
        path1_deleted=values["Path1"][2],
        path2_new=values["Path2"][0],
        path2_modified=values["Path2"][1],
        path2_deleted=values["Path2"][2],
        resync_required=RESYNC_REQUIRED_MARKER in text,
        safety_abort=abort,
    )


def _find_safety_abort(text: str) -> str | None:
    """The reason bisync refused, in a form worth showing an operator.

    Handles both output shapes: plain text, and JSON where the "Safety abort"
    prefix lives in an `object` field separate from the message.
    """
    for line in text.splitlines():
        if SAFETY_ABORT_MARKER not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            message = str(payload.get("msg", "")).strip()
            prefix = str(payload.get("object", "")).strip()
            return f"{prefix}: {message}" if prefix else message
        # Plain text: "2026/.. ERROR : Safety abort: too many deletes (...)"
        return stripped.split("ERROR :", 1)[-1].strip()
    return None


def needs_resync(text: str) -> bool:
    """Whether output says this job cannot proceed without an explicit resync."""
    return RESYNC_REQUIRED_MARKER in text


def build_bisync_command(
    job: Job,
    prepared: Prepared,
    path1: str,
    path2: str,
    *,
    workdir: str,
    resync: bool = False,
    dry_run: bool = False,
    unattended: bool = False,
    archive: list[str] | None = None,
) -> list[str]:
    """The argv for a bidirectional run.

    `--max-delete` carries the job's percentage directly. It is a percentage for
    bisync and a count for sync, and using the count conversion here would
    disable the brake. See the module docstring.
    """
    args: list[str] = [
        "bisync",
        path1,
        path2,
        # Persistent, per SPEC 10.2. The default lives in a cache directory that
        # is neither persistent nor writable by the container's user.
        "--workdir",
        workdir,
        "--use-json-log",
        "-v",
        # A percentage here, unlike sync. Never resolve_max_delete().
        "--max-delete",
        str(job.max_delete_pct),
        # SPEC 10.4: the losing version is kept, never silently discarded.
        "--conflict-resolve",
        job.conflict_resolve.value,
        "--conflict-loser",
        "num",
    ]

    if resync:
        args.append("--resync")
    if dry_run:
        args.append("--dry-run")

    if unattended:
        # SPEC 10.5. Only for scheduled runs: a manual run has someone watching
        # who should see the real error rather than a silent retry.
        args += ["--resilient", "--recover"]

    if job.check_access:
        # bisync's own stale-mount guard: it aborts unless matching RCLONE_TEST
        # files are present on both sides. Covers SMB and SFTP, which the local
        # only sentinel of SPEC 6.4 cannot.
        args.append("--check-access")

    args += list(archive or [])
    args += _comparison_args_for_bisync(job) + filter_args(job) + performance_args(job)
    return prepared.argv(*args)


# Conflict policies that decide a winner by comparing modification times.
_TIME_BASED_POLICIES = frozenset({ConflictResolve.newer, ConflictResolve.older})


def modify_window_applies(job: Job) -> bool:
    """Whether --modify-window can be passed without breaking conflict handling.

    A nonzero --modify-window **disables** bisync's time-based conflict policies
    outright. Verified against rclone 1.74.4 with versions ten seconds apart and
    a one second window: with `--conflict-resolve newer` there is no winner, both
    versions are renamed to `.conflict1` and `.conflict2`, and the file disappears
    from its original name. Without the flag, or with `--modify-window 0`, the
    newer version wins and the loser is kept as `.conflict1`.

    Non-time policies such as path1 are unaffected.

    So the window is dropped when the job's policy needs it. Passing it would
    silently discard the policy the operator chose and make their file vanish on
    every conflict, which is a worse outcome than the clock-drift sensitivity the
    window exists to prevent. The job editor says so next to the field.
    """
    return job.conflict_resolve not in _TIME_BASED_POLICIES


def _comparison_args_for_bisync(job: Job) -> list[str]:
    args = comparison_args(job)
    if modify_window_applies(job):
        return args
    # Drop the flag and its value.
    trimmed: list[str] = []
    skip_next = False
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item == "--modify-window":
            skip_next = True
            continue
        trimmed.append(item)
    return trimmed


def endpoints(job: Job) -> tuple[str, str]:
    """Bisync's path1 and path2 are positional and symmetric, so direction does
    not reorder them the way it does for a one way sync."""
    return ALIAS_SOURCE, ALIAS_DEST


__all__ = [
    "RESYNC_REQUIRED_MARKER",
    "SAFETY_ABORT_MARKER",
    "BisyncDeltas",
    "build_bisync_command",
    "endpoints",
    "modify_window_applies",
    "needs_resync",
    "parse_deltas",
    "workdir_for",
]


class BisyncEngine(SyncEngine):
    """Planning for a bidirectional job.

    Separate from RcloneEngine because bisync is a different command with its
    own semantics, not a flag on `sync`. The two most important differences are
    both load bearing here:

    - `--max-delete` is a **percentage** for bisync and a count for sync, so the
      threshold a one way plan reports would be meaningless on this screen.
    - A dry run needs prior listings. bisync copies the real ones into `.lst-dry`
      files, so a job that has never been resynced cannot be previewed at all,
      and says so rather than reporting an empty plan.
    """

    name = "bisync"

    def plan(self, job: Job, *, box: SecretBox, settings: Settings) -> Plan:
        if job.direction != Direction.bidirectional:
            raise EngineError("BisyncEngine plans bidirectional jobs only.")

        if not job.bisync_initialized:
            raise EngineError(
                f"'{job.name}' has not had its first sync yet, so there is "
                "nothing to compare against and a preview would be empty rather "
                "than reassuring. Use First Sync on the job, then dry run it."
            )

        workdir = workdir_for(str(settings.bisync_dir), job.id)
        Path(workdir).mkdir(parents=True, exist_ok=True)

        # The same resolution the runner uses, so a preview and the run it
        # previews cannot disagree about which connection is which.
        source, dest, read_path, write_path = endpoints_and_paths(job)
        connections = {ALIAS_SOURCE: source, ALIAS_DEST: dest}

        try:
            with rcloneconf.prepare(connections, box=box, settings=settings) as prepared:
                path1 = prepared.endpoints[ALIAS_SOURCE].spec(read_path or None)
                path2 = prepared.endpoints[ALIAS_DEST].spec(write_path or None)
                argv = build_bisync_command(
                    job, prepared, path1, path2, workdir=workdir, dry_run=True
                )
                result = process.run(
                    argv,
                    env=prepared.env,
                    redactor=prepared.redactor,
                    timeout_seconds=PLAN_TIMEOUT_SECONDS,
                    log_label="bisync --dry-run",
                )
                command = result.command_line
        except RemoteConfigError as exc:
            raise EngineError(str(exc)) from exc

        text = result.stdout + "\n" + result.stderr
        deltas = parse_deltas(text)

        if deltas.resync_required:
            raise EngineError(
                f"'{job.name}' needs a first sync before it can be previewed. Its "
                "listing state is missing, which happens if the working directory "
                "was lost. Use First Sync to rebuild it."
            )

        plan = Plan(changes=parse_planned_changes(text), commands=[command])

        if deltas.safety_abort:
            # Not an error: it is the answer. rclone refused before changing
            # anything, and saying so is the whole point of a preview.
            plan.warnings.append(
                f"rclone would refuse this run: {deltas.safety_abort} "
                "Check that both endpoints are fully mounted. If the deletions "
                "are intended, raise the delete brake."
            )

        # The counts rclone reports per side are the authority. The per-file
        # lines are for display, and a wording change should cost detail rather
        # than making the totals wrong.
        counted = (
            deltas.path1_new
            + deltas.path1_modified
            + deltas.path1_deleted
            + deltas.path2_new
            + deltas.path2_modified
            + deltas.path2_deleted
        )
        if counted != len(plan.changes):
            plan.warnings.append(
                f"rclone counted {counted} changes but named {len(plan.changes)}. "
                "The totals are rclone's own; the list below may be incomplete."
            )

        return plan

    def execute(self, job: Job, *, box: SecretBox, settings: Settings) -> Plan:
        """Not used. A live bisync is streamed and cancelled by the job runner."""
        raise EngineError(
            "Live runs are driven by the job runner, not by calling execute directly."
        )
