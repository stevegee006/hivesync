"""Retention pruning, which is the only thing in this program with no undo.

Acceptance criterion: a prune with 30 days set removes archive directories older
than 30 days, keeps the newest, and touches nothing outside the archive base,
asserted on filesystem state rather than on a report.

Every test here asserts what is left on disk. A report saying two directories
were removed is not evidence that the right two went.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_db_engine
from app.jobs import retention
from app.models import (
    ArchiveLayout,
    Connection,
    ConnectionType,
    DeleteMode,
    Job,
    JobRun,
    RunMode,
    RunStatus,
    RunTrigger,
)
from app.preferences import Preferences
from tests.conftest import create_schema

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(settings: Settings):
    create_schema(settings)
    return sessionmaker(bind=create_db_engine(settings))()


def _job(session, dest: Path, archive_base: Path | None = None, **overrides) -> Job:  # noqa: ANN003
    source = Connection(name="src", type=ConnectionType.local, base_path="/s")
    destination = Connection(name="dst", type=ConnectionType.local, base_path=str(dest))
    session.add_all([source, destination])
    session.commit()
    fields: dict = {
        "name": "Nightly Media",
        "source_connection_id": source.id,
        "dest_connection_id": destination.id,
        "source_path": "",
        "dest_path": "",
        "delete_mode": DeleteMode.archive,
        "archive_layout": ArchiveLayout.timestamped_dir,
        # Named rather than defaulted: where the archive goes is archive.py's
        # job and is tested there. These tests are about what gets pruned.
        "archive_base": str(archive_base) if archive_base else None,
        "filters": {},
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    return job


def _archive_run(base: Path, days_ago: int, *, slug: str = "nightly-media") -> Path:
    """Create an archived run directory as archive.plan_for would name it."""
    stamp = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H-%M-%SZ")
    directory = base / slug / stamp
    (directory / "sub").mkdir(parents=True)
    (directory / "sub" / "gone.txt").write_text("archived\n", encoding="utf-8")
    return directory


def _names(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()} if path.is_dir() else set()


# --------------------------------------------------------------------------
# The criterion
# --------------------------------------------------------------------------


def test_only_directories_past_the_cutoff_are_removed(tmp_path: Path, db) -> None:
    dest = tmp_path / "media"
    dest.mkdir()
    archive_base = tmp_path / "media.hivesync-archive"
    old = _archive_run(archive_base, days_ago=40)
    older = _archive_run(archive_base, days_ago=31)
    recent = _archive_run(archive_base, days_ago=29)
    newest = _archive_run(archive_base, days_ago=1)

    job = _job(db, dest, archive_base)
    plan = retention.plan_archive(job, Preferences(archive_retention_days=30), now=NOW)
    report = retention.Report()
    retention.prune_archive(plan, report)

    assert not old.exists()
    assert not older.exists()
    assert recent.exists(), "29 days old must survive a 30 day retention"
    assert newest.exists()
    assert report.directories_removed == 2
    assert report.bytes_freed > 0


def test_nothing_outside_the_archive_base_is_touched(tmp_path: Path, db) -> None:
    dest = tmp_path / "media"
    (dest / "keep").mkdir(parents=True)
    (dest / "keep" / "real.txt").write_text("live data\n", encoding="utf-8")
    archive_base = tmp_path / "media.hivesync-archive"
    _archive_run(archive_base, days_ago=40)

    # A neighbour that is old, is a directory, and is not ours.
    neighbour = tmp_path / "someone-elses-backup"
    neighbour.mkdir()
    (neighbour / "important.txt").write_text("not ours\n", encoding="utf-8")

    job = _job(db, dest, archive_base)
    assert job.archive_base
    report = retention.run(
        db,
        Settings(secret_key="x" * 44, config_dir=tmp_path / "cfg"),
        Preferences(archive_retention_days=30),
        now=NOW,
    )

    assert (dest / "keep" / "real.txt").exists()
    assert (neighbour / "important.txt").exists()
    assert report.directories_removed == 1


def test_a_directory_we_did_not_write_is_left_alone(tmp_path: Path, db) -> None:
    """Only names matching the run stamp format are considered. Anything else in
    the archive belongs to someone else."""
    dest = tmp_path / "media"
    dest.mkdir()
    archive_base = tmp_path / "media.hivesync-archive"
    _archive_run(archive_base, days_ago=40)
    stranger = archive_base / "nightly-media" / "manual-copy-2019"
    stranger.mkdir(parents=True)
    (stranger / "notes.txt").write_text("mine\n", encoding="utf-8")

    job = _job(db, dest, archive_base)
    plan = retention.plan_archive(job, Preferences(archive_retention_days=30), now=NOW)
    retention.prune_archive(plan, retention.Report())

    assert stranger.exists()
    assert _names(archive_base / "nightly-media") == {"manual-copy-2019"}


# --------------------------------------------------------------------------
# When pruning refuses
# --------------------------------------------------------------------------


def test_no_retention_set_prunes_nothing(tmp_path: Path, db) -> None:
    dest = tmp_path / "media"
    dest.mkdir()
    archive_base = tmp_path / "media.hivesync-archive"
    old = _archive_run(archive_base, days_ago=400)

    job = _job(db, dest, archive_base)
    plan = retention.plan_archive(job, Preferences(), now=NOW)
    retention.prune_archive(plan, retention.Report())

    assert old.exists(), "retention is off by default and must delete nothing"
    assert plan.skipped_reason is not None


def test_the_job_value_overrides_the_global_default(tmp_path: Path, db) -> None:
    dest = tmp_path / "media"
    dest.mkdir()
    archive_base = tmp_path / "media.hivesync-archive"
    kept = _archive_run(archive_base, days_ago=40)

    job = _job(db, dest, archive_base, archive_retention_days=90)
    plan = retention.plan_archive(job, Preferences(archive_retention_days=7), now=NOW)
    retention.prune_archive(plan, retention.Report())

    assert kept.exists(), "the job asked for 90 days and the global default is not it"


def test_the_flat_layout_is_refused_with_a_reason(tmp_path: Path, db) -> None:
    """It has no per-run directory to age, so pruning it would mean deleting
    individual files by parsing names. Refused rather than guessed at."""
    dest = tmp_path / "media"
    dest.mkdir()
    job = _job(db, dest, tmp_path / "arc", archive_layout=ArchiveLayout.suffix)

    plan = retention.plan_archive(job, Preferences(archive_retention_days=1), now=NOW)

    assert plan.directories == []
    assert "flat directory" in (plan.skipped_reason or "")
    assert "by hand" in (plan.skipped_reason or "")


def test_a_remote_archive_is_reported_rather_than_pruned(tmp_path: Path, db) -> None:
    source = Connection(name="s", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="nas", type=ConnectionType.smb, host="nas", share="Media", base_path="")
    db.add_all([source, dest])
    db.commit()
    job = Job(
        name="Remote Job",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        source_path="",
        dest_path="",
        delete_mode=DeleteMode.archive,
        filters={},
    )
    db.add(job)
    db.commit()

    plan = retention.plan_archive(job, Preferences(archive_retention_days=1), now=NOW)

    assert plan.directories == []
    assert "remote endpoint" in (plan.skipped_reason or "")
    # The path is named, so it can be cleared by hand.
    assert "Media" in (plan.skipped_reason or "")


# --------------------------------------------------------------------------
# Logs and run history
# --------------------------------------------------------------------------


def test_logs_older_than_the_window_are_removed(tmp_path: Path) -> None:
    import os

    settings = Settings(secret_key="x" * 44, config_dir=tmp_path)
    settings.ensure_directories()
    log_dir = settings.log_dir / "3"
    log_dir.mkdir(parents=True)
    old = log_dir / "1.log"
    fresh = log_dir / "2.log"
    old.write_text("old\n", encoding="utf-8")
    fresh.write_text("new\n", encoding="utf-8")
    stale = (datetime.now(UTC) - timedelta(days=120)).timestamp()
    os.utime(old, (stale, stale))

    report = retention.Report()
    retention.prune_logs(settings, Preferences(log_retention_days=90), report)

    assert not old.exists()
    assert fresh.exists()
    assert report.logs_removed == 1


def test_the_size_cap_removes_oldest_first(tmp_path: Path) -> None:
    """Both caps are needed: one pathological run blows the size cap inside the
    retention window, and a quiet year blows the age limit without approaching
    the size cap."""
    import os

    settings = Settings(secret_key="x" * 44, config_dir=tmp_path)
    settings.ensure_directories()
    log_dir = settings.log_dir / "1"
    log_dir.mkdir(parents=True)
    payload = "x" * (3 * 1024 * 1024)
    for index in range(6):
        path = log_dir / f"{index}.log"
        path.write_text(payload, encoding="utf-8")
        stamp = (datetime.now(UTC) - timedelta(hours=10 - index)).timestamp()
        os.utime(path, (stamp, stamp))

    # 18 MB on disk against a 16 MB cap: the oldest goes, and only the oldest.
    report = retention.Report()
    retention.prune_logs(settings, Preferences(log_max_total_mb=16), report)

    remaining = sorted(path.name for path in log_dir.glob("*.log"))
    assert remaining == ["1.log", "2.log", "3.log", "4.log", "5.log"]
    assert report.logs_removed == 1


def test_run_history_is_capped_per_job(tmp_path: Path, db) -> None:
    dest = tmp_path / "media"
    dest.mkdir()
    job = _job(db, dest)
    for _ in range(15):
        db.add(
            JobRun(
                job_id=job.id,
                trigger=RunTrigger.manual,
                mode=RunMode.live,
                status=RunStatus.success,
            )
        )
    db.commit()

    report = retention.Report()
    retention.prune_runs(db, Preferences(run_history_keep=10), report)

    remaining = list(db.scalars(__import__("sqlalchemy").select(JobRun)))
    assert len(remaining) == 10
    assert report.runs_removed == 5
    # The most recent survive.
    assert min(run.id for run in remaining) == 6


def test_an_in_flight_run_is_never_pruned(tmp_path: Path, db) -> None:
    """It is not history yet, and deleting its row would lose the outcome of work
    that actually happened."""
    dest = tmp_path / "media"
    dest.mkdir()
    job = _job(db, dest)
    running = JobRun(
        job_id=job.id, trigger=RunTrigger.manual, mode=RunMode.live, status=RunStatus.running
    )
    db.add(running)
    for _ in range(15):
        db.add(
            JobRun(
                job_id=job.id,
                trigger=RunTrigger.manual,
                mode=RunMode.live,
                status=RunStatus.success,
            )
        )
    db.commit()
    running_id = running.id

    retention.prune_runs(db, Preferences(run_history_keep=10), retention.Report())

    assert db.get(JobRun, running_id) is not None
