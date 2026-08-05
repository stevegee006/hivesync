"""M4's acceptance criterion, in real time.

    a `*/2 * * * *` job runs twice in five minutes, survives a container
    restart, and never double-runs.

That is a wall-clock and process-lifecycle claim, so it is tested as one. This
module drives a real scheduler over real minutes with a real database. It takes
roughly five minutes, which is why it is marked integration and kept out of the
unit suite.

tests/test_scheduler.py covers the same mechanics instantly with a stubbed
runner. This one exists because "it fires on time" and "it survives a restart"
cannot be honestly asserted without letting the clock run.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine
from app.jobs.scheduler import JobScheduler
from app.models import (
    Connection,
    ConnectionType,
    Job,
    JobRun,
    RunStatus,
    RunTrigger,
)
from tests.conftest import create_schema, make_settings

pytestmark = pytest.mark.integration

# A */2 job fires on even minutes. Waiting 5 minutes guarantees at least two
# boundaries regardless of where in the minute the test starts.
WINDOW_SECONDS = 5 * 60 + 15


class RecordingRunner:
    """Marks runs finished immediately, so the schedule is what is under test
    rather than how long a sync takes."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self.submitted: list[int] = []

    def submit(self, run_id: int) -> None:
        self.submitted.append(run_id)
        session = self._factory()
        try:
            run = session.get(JobRun, run_id)
            if run is not None:
                run.status = RunStatus.success
                session.commit()
        finally:
            session.close()

    def shutdown(self) -> None:
        pass

    def cancel(self, run_id: int) -> bool:
        return False


def _make_job(session, root: Path, name: str = "every-two") -> Job:
    # Never actually read or written: the stub runners below stand in for a sync.
    source = Connection(name=f"{name}-src", type=ConnectionType.local, base_path=str(root))
    dest = Connection(name=f"{name}-dst", type=ConnectionType.local, base_path=str(root))
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name=name,
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        source_path="",
        dest_path="",
        filters={},
        schedule_cron="*/2 * * * *",
        timezone="UTC",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _runs(session, job_id: int) -> list[JobRun]:
    session.expire_all()
    return list(
        session.scalars(
            select(JobRun)
            .where(JobRun.job_id == job_id, JobRun.trigger == RunTrigger.schedule)
            .order_by(JobRun.id)
        )
    )


@pytest.mark.slow
def test_fires_twice_in_five_minutes_and_survives_a_restart(tmp_path: Path) -> None:
    """The criterion, end to end and in real time."""
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    session = factory()
    job = _make_job(session, tmp_path)

    runner = RecordingRunner(factory)
    scheduler = JobScheduler(factory, runner, settings=settings)  # type: ignore[arg-type]
    scheduler.start()

    deadline = time.monotonic() + WINDOW_SECONDS
    restarted = False
    try:
        while time.monotonic() < deadline:
            time.sleep(5)

            # Part way through, tear the scheduler down and build a new one from
            # the same database. That is what a container restart does.
            if not restarted and len(_runs(session, job.id)) >= 1:
                scheduler.shutdown()
                scheduler = JobScheduler(factory, runner, settings=settings)  # type: ignore[arg-type]
                scheduler.start()
                restarted = True
                assert scheduler.next_run_time(job.id) is not None, (
                    "the schedule did not survive the restart"
                )

            if restarted and len(_runs(session, job.id)) >= 2:
                break
    finally:
        scheduler.shutdown()

    fired = _runs(session, job.id)
    assert restarted, "the test never reached the restart"
    assert len(fired) >= 2, f"expected at least two fires in the window, saw {len(fired)}"

    # Never double-runs: no two scheduled runs share a fire minute, and no run
    # was ever skipped for overlapping, because each finished before the next.
    minutes = [run.started_at.replace(second=0, microsecond=0) for run in fired if run.started_at]
    assert len(minutes) == len(set(minutes)), f"two runs fired in the same minute: {minutes}"
    assert all(run.status != RunStatus.skipped for run in fired)


@pytest.mark.slow
def test_a_long_running_job_is_skipped_rather_than_doubled(tmp_path: Path) -> None:
    """Never double-runs, under the condition that actually causes it: a sync
    still going when the next fire arrives.

    SPEC 6.2 asks for a recorded skip rather than a queued backlog.
    """
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    session = factory()
    job = _make_job(session, tmp_path, name="slow-job")

    class NeverFinishes:
        """Leaves every run in the running state, like a sync that is still going."""

        def __init__(self) -> None:
            self.submitted: list[int] = []

        def submit(self, run_id: int) -> None:
            self.submitted.append(run_id)
            inner = factory()
            try:
                run = inner.get(JobRun, run_id)
                if run is not None:
                    run.status = RunStatus.running
                    inner.commit()
            finally:
                inner.close()

        def shutdown(self) -> None:
            pass

        def cancel(self, run_id: int) -> bool:
            return False

    runner = NeverFinishes()
    scheduler = JobScheduler(factory, runner, settings=settings)  # type: ignore[arg-type]
    scheduler.start()

    deadline = time.monotonic() + WINDOW_SECONDS
    try:
        while time.monotonic() < deadline:
            time.sleep(5)
            if any(run.status == RunStatus.skipped for run in _runs(session, job.id)):
                break
    finally:
        scheduler.shutdown()

    fired = _runs(session, job.id)
    started = [run for run in fired if run.status == RunStatus.running]
    skipped = [run for run in fired if run.status == RunStatus.skipped]

    assert len(started) == 1, "a second run started while the first was still going"
    assert len(skipped) >= 1, "the blocked fire was not recorded"
    assert skipped[0].skip_reason == "A previous run was still in progress."
    # Exactly one thing was ever handed to the runner.
    assert len(runner.submitted) == 1
