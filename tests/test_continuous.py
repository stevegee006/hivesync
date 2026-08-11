"""Continuous mode: the backoff, the guards, and the quiet period.

The design constraint everything follows from: no backend here can announce a
change. Verified against rclone 1.74.4, `ChangeNotify` is false for local, sftp,
ftp and smb, so continuous means polling, and polling a NAS is expensive enough
that the backoff is the feature rather than a detail of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.crypto import SecretBox
from app.db import create_db_engine
from app.engines.rclone import quiet_period_args
from app.jobs.runner import LiveRunner, _prune_quiet_cycle
from app.jobs.watcher import ContinuousWatcher, WatchState, next_interval, should_keep
from app.models import (
    Connection,
    ConnectionType,
    Direction,
    Job,
    JobRun,
    RunMode,
    RunStatus,
    RunTrigger,
)
from app.schemas.job import JobCreate
from tests.conftest import create_schema, make_settings


def _session(settings: Settings) -> Session:
    return sessionmaker(bind=create_db_engine(settings))()


def _job(session: Session, **overrides) -> Job:  # noqa: ANN003
    source = Connection(name="s", type=ConnectionType.local, base_path="/data/source")
    dest = Connection(name="d", type=ConnectionType.local, base_path="/data/dest")
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "Watched",
        "source_connection_id": source.id,
        "dest_connection_id": dest.id,
        "continuous": True,
        "continuous_interval_seconds": 60,
        "continuous_idle_interval_seconds": 900,
        "quiet_period_seconds": 30,
        "filters": {},
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    return job


# --------------------------------------------------------------------------
# The backoff
# --------------------------------------------------------------------------


def test_a_cycle_that_changed_something_returns_to_the_floor() -> None:
    """Whoever is writing is probably still writing."""
    job = Job(continuous_interval_seconds=60, continuous_idle_interval_seconds=900)
    state = WatchState(interval=480.0)

    assert next_interval(job, state, changed=True) == 60
    assert state.consecutive_quiet == 0


def test_quiet_cycles_widen_towards_the_ceiling() -> None:
    job = Job(continuous_interval_seconds=60, continuous_idle_interval_seconds=900)
    state = WatchState(interval=60.0)

    assert next_interval(job, state, changed=False) == 120
    assert next_interval(job, state, changed=False) == 240
    assert next_interval(job, state, changed=False) == 480
    assert next_interval(job, state, changed=False) == 900
    # And stops there rather than growing without limit.
    assert next_interval(job, state, changed=False) == 900


def test_the_floor_is_never_below_five_seconds() -> None:
    """Each poll lists both endpoints in full, so a one second loop is a way to
    keep a NAS permanently busy for no benefit."""
    job = Job(continuous_interval_seconds=1, continuous_idle_interval_seconds=1)
    state = WatchState(interval=1.0)

    assert next_interval(job, state, changed=True) == 5


# --------------------------------------------------------------------------
# What gets kept in the history
# --------------------------------------------------------------------------


def test_a_cycle_that_moved_nothing_is_not_history() -> None:
    run = JobRun(status=RunStatus.success, files_transferred=0, files_deleted=0, errors_count=0)
    assert should_keep(run) is False


@pytest.mark.parametrize(
    "run",
    [
        JobRun(status=RunStatus.success, files_transferred=3),
        JobRun(status=RunStatus.success, files_deleted=1),
        JobRun(status=RunStatus.failed),
        JobRun(status=RunStatus.cancelled),
        JobRun(status=RunStatus.success, errors_count=2),
    ],
)
def test_anything_that_happened_is_kept(run: JobRun) -> None:
    """A failure especially. A refusal by the delete brake is exactly the run
    someone goes looking for later."""
    assert should_keep(run) is True


def test_the_runner_prunes_a_quiet_cycle(settings: Settings) -> None:
    create_schema(settings)
    session = _session(settings)
    job = _job(session)
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.schedule,
        mode=RunMode.live,
        status=RunStatus.success,
    )
    session.add(run)
    session.commit()
    run_id = run.id

    _prune_quiet_cycle(session, run, job)

    assert session.get(JobRun, run_id) is None
    assert job.last_checked_at is not None


def test_the_runner_keeps_a_cycle_that_did_something(settings: Settings) -> None:
    create_schema(settings)
    session = _session(settings)
    job = _job(session)
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.schedule,
        mode=RunMode.live,
        status=RunStatus.success,
        files_transferred=2,
    )
    session.add(run)
    session.commit()
    run_id = run.id

    _prune_quiet_cycle(session, run, job)

    assert session.get(JobRun, run_id) is not None


def test_a_scheduled_job_keeps_every_run(settings: Settings) -> None:
    """Pruning is a continuous-mode concession, not a general one: a scheduled
    run that changed nothing is still a record that the schedule fired."""
    create_schema(settings)
    session = _session(settings)
    job = _job(session, continuous=False)
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.schedule,
        mode=RunMode.live,
        status=RunStatus.success,
    )
    session.add(run)
    session.commit()
    run_id = run.id

    _prune_quiet_cycle(session, run, job)

    assert session.get(JobRun, run_id) is not None


# --------------------------------------------------------------------------
# The quiet period
# --------------------------------------------------------------------------


def test_the_quiet_period_becomes_min_age() -> None:
    """rclone skips anything modified more recently, so a file still being
    written is picked up on a later cycle instead of copied half finished."""
    job = Job(continuous=True, quiet_period_seconds=30)
    assert quiet_period_args(job) == ["--min-age", "30s"]


def test_a_scheduled_job_gets_the_quiet_period_too() -> None:
    """It used to be continuous only, on the reasoning that skipping recent
    files would surprise someone who had just pressed Run. That is backwards for
    the case it exists for: a download client writing into the source directory
    does not know a schedule fired at 2am, and half a file copied unattended is
    worse than a file collected on the next run.

    Existing jobs were migrated to zero, so none of them changed silently."""
    assert quiet_period_args(Job(continuous=False, quiet_period_seconds=30)) == [
        "--min-age",
        "30s",
    ]


def test_a_quiet_period_of_zero_is_off() -> None:
    assert quiet_period_args(Job(continuous=True, quiet_period_seconds=0)) == []


# --------------------------------------------------------------------------
# The guards
# --------------------------------------------------------------------------


def _payload(**overrides) -> dict:  # noqa: ANN003
    fields = {
        "name": "j",
        "source_connection_id": 1,
        "dest_connection_id": 2,
        "continuous": True,
    }
    fields.update(overrides)
    return fields


def test_continuous_and_bidirectional_is_refused() -> None:
    """bisync lists both sides and carries workdir state, so it is the most
    expensive thing to loop and the hardest to recover from."""
    with pytest.raises(ValidationError) as excinfo:
        JobCreate(**_payload(direction=Direction.bidirectional))

    message = str(excinfo.value)
    assert "not available for bidirectional" in message
    assert "one way" in message


def test_continuous_and_a_schedule_is_refused() -> None:
    with pytest.raises(ValidationError, match="either continuous or scheduled"):
        JobCreate(**_payload(schedule_cron="0 2 * * *"))


def test_an_idle_interval_below_the_floor_is_refused() -> None:
    with pytest.raises(ValidationError, match="cannot be shorter"):
        JobCreate(**_payload(continuous_interval_seconds=600, continuous_idle_interval_seconds=60))


def test_a_scheduled_job_is_unaffected_by_the_continuous_rules() -> None:
    job = JobCreate(
        name="j",
        source_connection_id=1,
        dest_connection_id=2,
        schedule_cron="0 2 * * *",
        direction=Direction.bidirectional,
        delete_mode="none",
    )
    assert job.continuous is False


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_the_watcher_starts_a_due_job_and_then_waits(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    session = factory()
    job = _job(session)

    started: list[int] = []

    class _Recording(LiveRunner):
        def submit(self, run_id: int) -> None:  # no rclone, just record the ask
            started.append(run_id)

    watcher = ContinuousWatcher(
        factory,
        _Recording(factory, box=SecretBox(settings.secret_key), settings=settings),
        settings=settings,
    )

    assert len(watcher.tick(now=0.0)) == 1
    # Not due again until the interval passes.
    assert watcher.tick(now=1.0) == []
    assert len(started) == 1

    session.expire_all()
    assert session.get(Job, job.id).last_checked_at is not None


def test_a_disabled_job_is_not_watched(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    _job(factory(), enabled=False)

    watcher = ContinuousWatcher(
        factory,
        LiveRunner(factory, box=SecretBox(settings.secret_key), settings=settings),
        settings=settings,
    )
    assert watcher.tick(now=0.0) == []


def test_an_overlapping_cycle_is_skipped_without_a_record(tmp_path: Path) -> None:
    """For a continuous job an overlap is the normal shape of a busy tree, not
    the anomaly a skipped scheduled run represents."""
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    session = factory()
    job = _job(session)
    session.add(
        JobRun(
            job_id=job.id,
            trigger=RunTrigger.schedule,
            mode=RunMode.live,
            status=RunStatus.running,
        )
    )
    session.commit()

    watcher = ContinuousWatcher(
        factory,
        LiveRunner(factory, box=SecretBox(settings.secret_key), settings=settings),
        settings=settings,
    )

    assert watcher.tick(now=0.0) == []
    # No skipped-run row: the history stays about what happened to files.
    assert session.query(JobRun).filter_by(status=RunStatus.skipped).count() == 0


def test_the_form_round_trips_continuous_settings(
    authed_client: TestClient, settings: Settings
) -> None:
    session = _session(settings)
    source = Connection(name="src", type=ConnectionType.local, base_path="/a")
    dest = Connection(name="dst", type=ConnectionType.local, base_path="/b")
    session.add_all([source, dest])
    session.commit()

    response = authed_client.post(
        "/jobs",
        data={
            "name": "Watched",
            "source_connection_id": str(source.id),
            "dest_connection_id": str(dest.id),
            "source_path": "",
            "dest_path": "",
            "direction": "source_to_dest",
            "compare_mode": "mtime_size",
            "modify_window": "1s",
            "delete_mode": "none",
            "max_delete_pct": "20",
            "timezone": "UTC",
            "notify_on": "failure",
            "enabled": "true",
            "continuous": "true",
            "continuous_interval_seconds": "30",
            "continuous_idle_interval_seconds": "600",
            "quiet_period_seconds": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    job = session.query(Job).filter_by(name="Watched").one()
    assert job.continuous is True
    assert job.continuous_interval_seconds == 30
    assert job.continuous_idle_interval_seconds == 600
    # Zero is a real choice and must survive, not fall back to the default.
    assert job.quiet_period_seconds == 0
