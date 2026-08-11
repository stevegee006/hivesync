"""Putting an archived deletion back.

SPEC open question 3 asked whether archived deletions should be tracked as
restorable entries or whether the folder on disk was enough. The folder was not
enough: knowing a file is *somewhere* under a timestamped directory is not the
same as being able to get it back, and the run that archived it never recorded
where it went.

Two properties hold this together, and both are load bearing.

**A restore never overwrites.** `--ignore-existing` is passed on every copy, so
a file that exists at the target now is left exactly as it is and reported as
skipped. Verified against rclone 1.74.4: with the flag set and a different file
already in place, the current version was preserved. Restoring is meant to
recover something that was removed, and the one thing worse than a missing file
is a present one silently replaced by an older copy.

**The archive is read, never written.** Restore copies out of it and leaves it
alone, so the same file can be restored again, and a mistake here cannot destroy
the only remaining copy. Deleting from the archive is retention's job, and it is
the one operation in this program with nothing behind it.

Only the timestamped layout is restorable. The flat suffix layout puts every
run's files in one directory distinguished by a `.<stamp>` suffix on the name,
so reversing it means parsing a filename to guess where it came from. Retention
declines to prune that layout for the same reason, and guessing is not a thing
to do with someone's data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings
from app.crypto import SecretBox
from app.engines import process, rcloneconf
from app.engines.rclone import endpoints_and_paths
from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE
from app.models import ArchiveLayout, ChangeSide, Job, JobRun

logger = logging.getLogger(__name__)

LIST_TIMEOUT_SECONDS = 5 * 60
RESTORE_TIMEOUT_SECONDS = 60 * 60

# Enough to fill a page without listing a tree nobody will read through.
MAX_LISTED = 2000


class RestoreError(Exception):
    """Restoring is not possible, with a reason for the operator."""


@dataclass(frozen=True)
class ArchivedFile:
    """One file sitting in a run's archive, and where it came from."""

    path: str
    side: ChangeSide

    @property
    def key(self) -> str:
        """Identifies the file in a form, side included."""
        return f"{self.side.value}:{self.path}"


@dataclass
class RestoreReport:
    """What a restore did. Skipped is not a failure: it is the guard working."""

    restored: list[str]
    skipped: list[str]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def why_not(job: Job, run: JobRun) -> str | None:
    """The reason this run cannot be restored from, or None if it can."""
    if job.archive_layout == ArchiveLayout.suffix:
        return (
            "This job archives with the flat suffix layout, which puts every "
            "run's files in one directory and marks them with a timestamp on "
            "the name. Working out where one came from means reading its "
            "filename, so restoring is not offered. The files are in the "
            "archive directory and can be moved back by hand."
        )
    if not run.archive_dirs:
        return (
            "This run predates HiveSync recording where it archived to, so "
            "there is no directory to restore from. Its files are still in the "
            "archive under this job's name. Runs from now on can be restored."
        )
    return None


def _specs(job: Job, run: JobRun, prepared: rcloneconf.Prepared) -> dict[ChangeSide, str]:
    """Archive directory and live endpoint for each side, as rclone specs.

    The stored path carries no remote, because the alias it was written with
    belonged to a process that has ended. It is reattached here from this
    process's endpoints.
    """
    _source, _dest, read_path, write_path = endpoints_and_paths(job)
    endpoints = {
        ChangeSide.source: (ALIAS_SOURCE, read_path),
        ChangeSide.dest: (ALIAS_DEST, write_path),
    }
    resolved: dict[ChangeSide, str] = {}
    for side, stored in (run.archive_dirs or {}).items():
        key = ChangeSide(side)
        alias, _subpath = endpoints[key]
        resolved[key] = f"{prepared.endpoints[alias].alias}:{stored}"
    return resolved


def _live_root(job: Job, side: ChangeSide, prepared: rcloneconf.Prepared) -> str:
    """Where a file restored from this side belongs."""
    _source, _dest, read_path, write_path = endpoints_and_paths(job)
    if side is ChangeSide.source:
        return prepared.endpoints[ALIAS_SOURCE].spec(read_path or None)
    return prepared.endpoints[ALIAS_DEST].spec(write_path or None)


def list_archived(
    job: Job, run: JobRun, *, box: SecretBox, settings: Settings
) -> list[ArchivedFile]:
    """What is actually in this run's archive, right now.

    Listed rather than read from the run's change rows. The rows record what the
    run archived; the directory records what is still there, and retention or a
    person may have removed some of it in between. Offering a restore for a file
    that is gone would fail at the moment someone needed it to work.
    """
    reason = why_not(job, run)
    if reason:
        raise RestoreError(reason)

    source, dest, _read, _write = endpoints_and_paths(job)
    found: list[ArchivedFile] = []
    with rcloneconf.prepare(
        {ALIAS_SOURCE: source, ALIAS_DEST: dest}, box=box, settings=settings
    ) as prepared:
        for side, archive_spec in _specs(job, run, prepared).items():
            result = process.run(
                prepared.argv("lsf", "--recursive", "--files-only", archive_spec),
                env=prepared.env,
                redactor=prepared.redactor,
                timeout_seconds=LIST_TIMEOUT_SECONDS,
                log_label="lsf archive",
            )
            # A missing directory is not an error worth raising: the run may
            # have archived nothing, or retention may have pruned it.
            if not result.ok:
                logger.info(
                    "Archive directory could not be listed",
                    extra={"run_id": run.id, "side": side.value},
                )
                continue
            for line in result.stdout.splitlines():
                path = line.strip()
                if path:
                    found.append(ArchivedFile(path=path, side=side))
    return sorted(found, key=lambda item: (item.side.value, item.path))[:MAX_LISTED]


def restore(
    job: Job,
    run: JobRun,
    wanted: list[ArchivedFile],
    *,
    box: SecretBox,
    settings: Settings,
) -> RestoreReport:
    """Copy files out of the archive and back where they came from.

    One `copyto` per file rather than a batch, because the report has to say
    which files came back and which were left alone, and a batch reports a
    total. An archive restore is a handful of files by nature: the case is "I
    deleted the wrong folder", not "move a million objects".
    """
    reason = why_not(job, run)
    if reason:
        raise RestoreError(reason)

    report = RestoreReport(restored=[], skipped=[], errors=[])
    if not wanted:
        return report

    source, dest, _read, _write = endpoints_and_paths(job)
    with rcloneconf.prepare(
        {ALIAS_SOURCE: source, ALIAS_DEST: dest}, box=box, settings=settings
    ) as prepared:
        archives = _specs(job, run, prepared)
        for item in wanted:
            archive_spec = archives.get(item.side)
            if archive_spec is None:
                report.errors.append(f"{item.path}: this run archived nothing on that side.")
                continue

            from_path = f"{archive_spec.rstrip('/')}/{item.path}"
            to_path = f"{_live_root(job, item.side, prepared).rstrip('/')}/{item.path}"
            result = process.run(
                prepared.argv(
                    "copyto",
                    from_path,
                    to_path,
                    # The whole safety property. Verified against rclone 1.74.4:
                    # with a different file already in place, the current one is
                    # preserved and the copy is skipped.
                    "--ignore-existing",
                    "--use-json-log",
                    "-v",
                ),
                env=prepared.env,
                redactor=prepared.redactor,
                timeout_seconds=RESTORE_TIMEOUT_SECONDS,
                log_label="copyto restore",
            )
            if not result.ok:
                report.errors.append(f"{item.path}: {result.stderr.strip()[:200]}")
                continue
            # Detected on the positive signal, because there is no negative
            # one. Verified against rclone 1.74.4: a copy logs
            # `Copied (new) to: <name>` and a skip logs **nothing at all**, not
            # even at -v. Looking for a "skipped" message finds nothing and
            # reports every restore as successful, including the ones that did
            # not happen.
            #
            # "Copied" as a substring, not a prefix: a file over the
            # multi-thread cutoff logs "Multi-thread Copied", which is the same
            # trap the run summary fell into.
            if "Copied" in result.stdout + result.stderr:
                report.restored.append(item.path)
            else:
                report.skipped.append(item.path)

    logger.info(
        "Restore finished",
        extra={
            "run_id": run.id,
            "job": job.name,
            "restored": len(report.restored),
            "skipped": len(report.skipped),
            "errors": len(report.errors),
        },
    )
    return report
