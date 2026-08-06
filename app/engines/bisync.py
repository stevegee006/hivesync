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

from app.engines.rclone import comparison_args, filter_args, performance_args
from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE, Prepared
from app.models import ConflictResolve, Job

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
