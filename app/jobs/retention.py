"""Scheduled maintenance: archive pruning, log pruning, run history pruning.

Archive pruning is the only operation in this program with nothing behind it. A
sync can be re-run, a deletion can be recovered from the archive, but a pruned
archive entry is gone. It is written accordingly:

- **Off unless a number is set.** No default retention, per job or globally.
- **Only whole run directories, only under the resolved archive base.** The
  timestamped layout is what makes this safe: `<base>/<job-slug>/<stamp>/` is a
  unit created by one run, so the decision is per run rather than per file, and
  the stamp is the age.
- **The suffix layout is not pruned at all.** It puts every version into one flat
  directory distinguished by a name suffix, so pruning would mean parsing ages
  out of filenames and deleting individual files. Refused rather than guessed at.
- **Local paths only.** Deleting over SMB or SFTP means an rclone `purge` against
  a remote whose contents this process cannot verify first. A remote archive is
  reported as unprunable, with the path, so it can be cleared by hand.
- **Dry run first.** `plan()` returns what would go; `prune()` takes that plan.
  The Settings screen shows the plan before anything runs.

Log and run-history pruning are ordinary housekeeping and carry no such risk:
both are our own artefacts under /config.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.engines import rclone, rcloneconf
from app.jobs import archive
from app.models import ArchiveLayout, DeleteMode, Job, JobRun, RunStatus, utcnow
from app.preferences import Preferences

logger = logging.getLogger(__name__)

# The timestamp format archive.plan_for writes, and the only directory name this
# module will consider deleting.
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
STAMP_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


@dataclass
class ArchivePlan:
    """What pruning would remove for one job, and what it refuses to touch."""

    job_id: int
    job_name: str
    retention_days: int
    directories: list[Path] = field(default_factory=list)
    bytes_freed: int = 0
    # Why nothing will happen, when nothing will. User facing.
    skipped_reason: str | None = None


@dataclass
class Report:
    """The outcome of one maintenance pass."""

    archives: list[ArchivePlan] = field(default_factory=list)
    directories_removed: int = 0
    bytes_freed: int = 0
    logs_removed: int = 0
    runs_removed: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Archive pruning
# --------------------------------------------------------------------------


def retention_for(job: Job, preferences: Preferences) -> int | None:
    """The per-job value, falling back to the global default. None means never."""
    if job.archive_retention_days is not None:
        return job.archive_retention_days
    return preferences.archive_retention_days


def plan_archive(job: Job, preferences: Preferences, *, now: datetime | None = None) -> ArchivePlan:
    """What pruning would delete for one job. Deletes nothing itself."""
    days = retention_for(job, preferences)
    plan = ArchivePlan(job_id=job.id, job_name=job.name, retention_days=days or 0)

    if job.delete_mode != DeleteMode.archive or not days:
        plan.skipped_reason = "Retention is not set for this job."
        return plan

    if job.archive_layout != ArchiveLayout.timestamped_dir:
        # See the module docstring: the flat layout has no run-sized unit to age.
        plan.skipped_reason = (
            "This job archives into one flat directory, so there are no per-run "
            "directories to age out. Pruning it would mean deleting individual "
            "files by name, which is not done automatically. Clear it by hand."
        )
        return plan

    try:
        _source, dest, _read, write = rclone.endpoints_and_paths(job)
        destination = rcloneconf.display_path(dest, write or None)
        base = archive.plan_for(job, destination).base
    except (archive.ArchiveError, rcloneconf.RemoteConfigError) as exc:
        plan.skipped_reason = f"The archive path could not be resolved: {exc}"
        return plan

    remote, path = _split(base)
    # is_absolute rather than a leading slash: "a path on this filesystem" is
    # what the check means, and a relative path is refused for the same reason a
    # remote one is, since there is nothing to resolve it against here.
    if remote or not Path(path).is_absolute():
        plan.skipped_reason = (
            f"The archive at '{base}' is on a remote endpoint. Automatic pruning "
            "only removes local directories, because a remote listing cannot be "
            "verified first. Clear it from the device that hosts it."
        )
        return plan

    root = Path(path) / archive.slugify(job.name)
    if not root.is_dir():
        plan.skipped_reason = "Nothing has been archived for this job yet."
        return plan

    cutoff = (now or utcnow()) - timedelta(days=days)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not STAMP.match(entry.name):
            # Anything not written by this program is left where it is.
            continue
        stamped = datetime.strptime(entry.name, STAMP_FORMAT).replace(tzinfo=cutoff.tzinfo)
        if stamped < cutoff:
            plan.directories.append(entry)
            plan.bytes_freed += _size_of(entry)
    return plan


def _split(spec: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z0-9_.\-]{2,}):(.*)$", spec)
    return (match.group(1), match.group(2)) if match else ("", spec)


def _size_of(directory: Path) -> int:
    total = 0
    for item in directory.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def prune_archive(plan: ArchivePlan, report: Report) -> None:
    """Carry out one job's plan. Nothing is recomputed here.

    The caller decides; this only executes, so what an operator was shown is
    exactly what happens.
    """
    for directory in plan.directories:
        try:
            shutil.rmtree(directory)
            report.directories_removed += 1
            logger.info(
                "Pruned archive directory",
                extra={"job": plan.job_name, "path": str(directory)},
            )
        except OSError as exc:
            report.errors.append(f"Could not remove {directory}: {exc.strerror or exc}")
    report.bytes_freed += plan.bytes_freed


# --------------------------------------------------------------------------
# Logs and run history
# --------------------------------------------------------------------------


def prune_logs(settings: Settings, preferences: Preferences, report: Report) -> None:
    """Age and size caps on per-run logs. SPEC section 16.

    Age first, then size, oldest first until under the cap. Both are needed: a
    single pathological run can blow the size cap inside the retention window,
    and a quiet year of small runs blows the age limit without ever approaching
    the size cap.
    """
    log_dir = settings.log_dir
    if not log_dir.is_dir():
        return

    cutoff = (utcnow() - timedelta(days=preferences.log_retention_days)).timestamp()
    files: list[tuple[float, int, Path]] = []
    for path in log_dir.rglob("*.log"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            if _unlink(path, report):
                report.logs_removed += 1
            continue
        files.append((stat.st_mtime, stat.st_size, path))

    budget = preferences.log_max_total_mb * 1024 * 1024
    total = sum(size for _mtime, size, _path in files)
    for _mtime, size, path in sorted(files):
        if total <= budget:
            break
        if _unlink(path, report):
            report.logs_removed += 1
            total -= size


def _unlink(path: Path, report: Report) -> bool:
    try:
        path.unlink()
    except OSError as exc:
        report.errors.append(f"Could not remove {path}: {exc.strerror or exc}")
        return False
    return True


def prune_runs(session: Session, preferences: Preferences, report: Report) -> None:
    """Keep the most recent N runs per job.

    Never touches a run that has not finished: an in-flight run is not history,
    and deleting its row while the runner holds it would lose the outcome of work
    that actually happened.
    """
    keep = preferences.run_history_keep
    for (job_id,) in session.execute(select(Job.id)):
        doomed = list(
            session.scalars(
                select(JobRun.id)
                .where(
                    JobRun.job_id == job_id,
                    JobRun.status.not_in([RunStatus.queued, RunStatus.running]),
                )
                .order_by(JobRun.id.desc())
                .offset(keep)
            )
        )
        if not doomed:
            continue
        session.execute(delete(JobRun).where(JobRun.id.in_(doomed)))
        report.runs_removed += len(doomed)
    session.commit()


# --------------------------------------------------------------------------
# The pass the scheduler runs
# --------------------------------------------------------------------------


def plan(session: Session, preferences: Preferences, *, now: datetime | None = None) -> Report:
    """What a maintenance pass would do. Changes nothing."""
    report = Report()
    for job in session.scalars(select(Job)):
        report.archives.append(plan_archive(job, preferences, now=now))
    return report


def run(
    session: Session,
    settings: Settings,
    preferences: Preferences,
    *,
    now: datetime | None = None,
) -> Report:
    """One maintenance pass: plan, then act on the plan."""
    report = plan(session, preferences, now=now)
    for archive_plan in report.archives:
        prune_archive(archive_plan, report)
    prune_logs(settings, preferences, report)
    prune_runs(session, preferences, report)

    logger.info(
        "Maintenance pass finished",
        extra={
            "directories_removed": report.directories_removed,
            "logs_removed": report.logs_removed,
            "runs_removed": report.runs_removed,
            "errors": len(report.errors),
        },
    )
    return report
