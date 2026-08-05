"""Scheduler behaviour.

M4's criterion is a wall-clock claim: "runs twice in five minutes, survives a
container restart, and never double-runs". These tests establish the mechanics
without waiting five minutes; tests/test_scheduler_integration.py then proves it
in real time against a real container.

The runner is stubbed so nothing here actually syncs. What is under test is which
runs get created and which get skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine
from app.jobs import planner
from app.jobs.scheduler import JobScheduler, job_key
from app.models import (
    Connection,
    ConnectionType,
    Job,
    JobRun,
    RunMode,
    RunStatus,
    RunTrigger,
)
from tests.conftest import create_schema, make_settings


class StubRunner:
    """Stands in for LiveRunner. Records submissions instead of syncing."""

    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, run_id: int) -> None:
        self.submitted.append(run_id)

    def shutdown(self) -> None:
        pass

    def cancel(self, run_id: int) -> bool:
        return False


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    return settings, factory, factory()


def _job(session, **overrides) -> Job:  # noqa: ANN003
    # Names derive from the job so a test can create more than one.
    label = str(overrides.get("name", "scheduled"))
    source = Connection(name=f"{label}-src", type=ConnectionType.local, base_path="/a")
    dest = Connection(name=f"{label}-dst", type=ConnectionType.local, base_path="/b")
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "scheduled",
        "source_connection_id": source.id,
        "dest_connection_id": dest.id,
        "source_path": "",
        "dest_path": "",
        "filters": {},
        "schedule_cron": "*/2 * * * *",
        "timezone": "UTC",
        "enabled": True,
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _scheduler(env, runner: StubRunner) -> JobScheduler:
    settings, factory, _session = env
    return JobScheduler(factory, runner, settings=settings)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Registration, and restart survival
# --------------------------------------------------------------------------


def test_scheduled_jobs_are_registered_from_the_database(env) -> None:
    """Restart survival: the schedule comes from the Job table, which is the
    only place it was ever stored."""
    _settings, _factory, session = env
    job = _job(session)
    scheduler = _scheduler(env, StubRunner())
    scheduler.start()
    try:
        assert scheduler.next_run_time(job.id) is not None
    finally:
        scheduler.shutdown()


def test_a_fresh_scheduler_rebuilds_the_same_schedule(env) -> None:
    """What a container restart actually does: a brand new scheduler, same
    database, same resulting schedule."""
    _settings, _factory, session = env
    job = _job(session)

    first = _scheduler(env, StubRunner())
    first.start()
    before = first.next_run_time(job.id)
    first.shutdown()

    second = _scheduler(env, StubRunner())
    second.start()
    try:
        assert second.next_run_time(job.id) is not None
        assert before is not None
    finally:
        second.shutdown()


def test_jobs_without_a_schedule_are_not_registered(env) -> None:
    _settings, _factory, session = env
    job = _job(session, schedule_cron=None)
    scheduler = _scheduler(env, StubRunner())
    scheduler.start()
    try:
        assert scheduler.next_run_time(job.id) is None
    finally:
        scheduler.shutdown()


def test_disabled_jobs_keep_their_schedule_but_do_not_fire(env) -> None:
    _settings, _factory, session = env
    job = _job(session, enabled=False)
    scheduler = _scheduler(env, StubRunner())
    scheduler.start()
    try:
        assert scheduler.next_run_time(job.id) is None
    finally:
        scheduler.shutdown()
    # The expression is still on the job, ready for when it is re-enabled.
    session.refresh(job)
    assert job.schedule_cron == "*/2 * * * *"


def test_an_unparseable_schedule_does_not_stop_the_others(env) -> None:
    """A row edited by hand must not take every other job down with it."""
    _settings, _factory, session = env
    good = _job(session, name="good")
    # Bypasses schema validation the way a direct database edit would.
    bad = _job(session, name="bad")
    bad.schedule_cron = "not a cron"
    session.commit()

    scheduler = _scheduler(env, StubRunner())
    scheduler.start()
    try:
        assert scheduler.next_run_time(good.id) is not None
        assert scheduler.next_run_time(bad.id) is None
    finally:
        scheduler.shutdown()


def test_reload_picks_up_an_edited_schedule(env) -> None:
    _settings, _factory, session = env
    job = _job(session, schedule_cron=None)
    scheduler = _scheduler(env, StubRunner())
    scheduler.start()
    try:
        assert scheduler.next_run_time(job.id) is None
        job.schedule_cron = "0 3 * * *"
        session.commit()
        scheduler.reload()
        assert scheduler.next_run_time(job.id) is not None
    finally:
        scheduler.shutdown()


def test_reload_drops_a_deleted_job(env) -> None:
    _settings, _factory, session = env
    job = _job(session)
    job_id = job.id
    scheduler = _scheduler(env, StubRunner())
    scheduler.start()
    try:
        assert scheduler.next_run_time(job_id) is not None
        session.delete(job)
        session.commit()
        scheduler.reload()
        assert scheduler.next_run_time(job_id) is None
    finally:
        scheduler.shutdown()


def test_job_key_is_stable(env) -> None:
    assert job_key(7) == "hivesync-job-7"


# --------------------------------------------------------------------------
# Never double-runs
# --------------------------------------------------------------------------


def test_firing_creates_a_live_run(env) -> None:
    _settings, _factory, session = env
    job = _job(session)
    runner = StubRunner()
    scheduler = _scheduler(env, runner)

    scheduler._fire(job.id)

    runs = list(session.scalars(select(JobRun).where(JobRun.job_id == job.id)))
    assert len(runs) == 1
    assert runs[0].trigger == RunTrigger.schedule
    assert runs[0].mode == RunMode.live
    assert runner.submitted == [runs[0].id]


def test_a_fire_during_an_active_run_records_a_skip(env) -> None:
    """SPEC 6.2: record a skipped run rather than queueing indefinitely.

    Silently dropping it would leave an operator wondering why a scheduled sync
    never happened.
    """
    _settings, _factory, session = env
    job = _job(session)
    planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)

    runner = StubRunner()
    scheduler = _scheduler(env, runner)
    scheduler._fire(job.id)

    runs = list(session.scalars(select(JobRun).where(JobRun.job_id == job.id)))
    skipped = [run for run in runs if run.status == RunStatus.skipped]
    assert len(skipped) == 1
    assert skipped[0].skip_reason == "A previous run was still in progress."
    assert skipped[0].trigger == RunTrigger.schedule
    # Nothing was handed to the runner, so nothing double-ran.
    assert runner.submitted == []


def test_a_skipped_run_does_not_block_the_next_fire(env) -> None:
    """A skipped row is terminal, so it must not itself count as active."""
    _settings, _factory, session = env
    job = _job(session)
    active = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)

    runner = StubRunner()
    scheduler = _scheduler(env, runner)
    scheduler._fire(job.id)
    assert runner.submitted == []

    # The earlier run finishes.
    active.status = RunStatus.success
    session.commit()

    scheduler._fire(job.id)
    assert len(runner.submitted) == 1


def test_a_disabled_job_does_not_fire_even_if_triggered(env) -> None:
    _settings, _factory, session = env
    job = _job(session, enabled=False)
    runner = StubRunner()
    _scheduler(env, runner)._fire(job.id)
    assert runner.submitted == []
    assert session.scalars(select(JobRun)).all() == []


def test_firing_a_deleted_job_is_harmless(env) -> None:
    _settings, _factory, session = env
    job = _job(session)
    job_id = job.id
    session.delete(job)
    session.commit()

    runner = StubRunner()
    _scheduler(env, runner)._fire(job_id)
    assert runner.submitted == []
