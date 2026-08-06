"""The live output stream, and the three ways it used to hang.

Every test here has a hard time limit. A hang is the bug under test, so a test
that waits for it would hang the suite instead of failing it.

What went wrong, all of it reported as "the UI hangs":

1. A dry run published nothing to the broker and never called `finish`, so the
   run detail page showed a live pane that never updated and never reloaded.
2. Subscribing to a run that had already ended waited for an end signal that had
   been delivered to whoever was listening at the time, and never arrived.
3. After a restart the broker had no memory of any earlier run, so every
   historical run looked live to it and hung the same way.

Each one leaks a connection per visit, and browsers allow about six per host, so
a few of those and the application stops responding altogether.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.crypto import SecretBox
from app.db import create_db_engine
from app.jobs.events import RunBroker, RunEvent
from app.jobs.planner import PlanRunner
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

# Generous enough for a slow machine, short enough that a hang fails the run
# rather than waiting for the broker's 15 second heartbeat.
DEADLINE = 5.0


def _drain(iterator, limit: float = DEADLINE) -> list[RunEvent]:
    """Collect an iterator to exhaustion, failing if it does not end in time."""
    collected: list[RunEvent] = []
    error: list[BaseException] = []

    def consume() -> None:
        try:
            collected.extend(iterator)
        except BaseException as exc:
            error.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    worker.join(timeout=limit)
    if worker.is_alive():
        raise AssertionError(
            f"the stream was still open after {limit}s. It has to end by itself, "
            "or the browser holds the connection until it runs out."
        )
    if error:
        raise error[0]
    return collected


# --------------------------------------------------------------------------
# The broker
# --------------------------------------------------------------------------


def test_subscribing_after_the_run_ended_returns_at_once() -> None:
    broker = RunBroker()
    broker.publish(7, RunEvent(kind="line", text="working"))
    broker.finish(7)

    events = _drain(broker.subscribe(7))

    assert [event.kind for event in events] == []


def test_a_subscriber_present_at_the_end_is_released() -> None:
    broker = RunBroker()
    stream = broker.subscribe(7)
    broker.publish(7, RunEvent(kind="line", text="working"))

    finisher = threading.Timer(0.2, broker.finish, args=(7,))
    finisher.start()
    try:
        events = _drain(stream)
    finally:
        finisher.cancel()

    assert [event.text for event in events] == ["working"]


def test_the_backlog_can_be_read_without_subscribing() -> None:
    """What the endpoint uses for a run it already knows is over."""
    broker = RunBroker()
    broker.publish(7, RunEvent(kind="line", text="one"))
    broker.publish(7, RunEvent(kind="line", text="two"))

    assert [event.text for event in broker.backlog_for(7)] == ["one", "two"]
    # And reading it does not register a subscriber that would then be waited on.
    broker.finish(7)
    assert _drain(broker.subscribe(7)) == []


def test_finished_run_memory_is_bounded() -> None:
    """It only has to outlive a browser connecting to a run that just ended."""
    broker = RunBroker()
    for run_id in range(2000):
        broker.finish(run_id)

    # The oldest are forgotten, and forgetting them is safe: the endpoint checks
    # the database, which is what actually decides whether a run is live.
    assert _drain(broker.subscribe(1999)) == []
    assert len(broker._finished) <= 500


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


def _finished_run(settings, status: RunStatus = RunStatus.success) -> int:
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="s", type=ConnectionType.local, base_path="/data/source")
    dest = Connection(name="d", type=ConnectionType.local, base_path="/data/dest")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Watched",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
    )
    session.add(job)
    session.commit()
    run = JobRun(job_id=job.id, trigger=RunTrigger.manual, mode=RunMode.dry_run, status=status)
    session.add(run)
    session.commit()
    return run.id


@pytest.mark.parametrize(
    "status", [RunStatus.success, RunStatus.failed, RunStatus.cancelled, RunStatus.skipped]
)
def test_streaming_a_finished_run_closes_with_done(
    authed_client: TestClient, settings, status: RunStatus
) -> None:
    """Whatever it ended as. A failed run published no final event of its own,
    so the page sat on 'Running' until someone reloaded by hand."""
    run_id = _finished_run(settings, status)

    with authed_client.stream("GET", f"/api/runs/{run_id}/stream") as response:
        assert response.status_code == 200
        body = "".join(_drain(response.iter_text()))

    assert '"kind": "done"' in body
    assert status.value in body


def test_the_stream_survives_a_restart_with_no_broker_memory(
    authed_client: TestClient, settings
) -> None:
    """The broker is per process. After a restart it knows nothing about earlier
    runs, so the database has to be what decides whether one is still live."""
    run_id = _finished_run(settings)

    # Exactly the state a fresh process is in: nothing published, nothing
    # finished, and a run row that ended before this process existed. The broker
    # is a module level singleton, so the memory has to be cleared deliberately
    # rather than assumed empty.
    from app.jobs.events import broker

    broker._finished.pop(run_id, None)
    assert run_id not in broker._finished

    with authed_client.stream("GET", f"/api/runs/{run_id}/stream") as response:
        body = "".join(_drain(response.iter_text()))

    assert '"kind": "done"' in body


# --------------------------------------------------------------------------
# The dry run, which published nothing at all
# --------------------------------------------------------------------------


def test_a_dry_run_releases_its_watchers_even_when_the_run_vanishes(tmp_path: Path) -> None:
    """The reported bug: the planner never touched the broker, so a watcher was
    never told anything had ended.

    Driven through the path that needs no rclone. Deleting the job cascades to
    its runs, so the planner finds nothing to do, and even then the watcher has
    to be released rather than left waiting.
    """
    settings = make_settings(tmp_path)
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    session = factory()

    source = Connection(name="s", type=ConnectionType.local, base_path=str(tmp_path))
    dest = Connection(name="d", type=ConnectionType.local, base_path=str(tmp_path))
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Doomed",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
    )
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.manual,
        mode=RunMode.dry_run,
        status=RunStatus.queued,
    )
    session.add(run)
    session.commit()
    run_id = run.id

    session.delete(job)
    session.commit()

    from app.jobs.events import broker

    stream = broker.subscribe(run_id)
    runner = PlanRunner(factory, box=SecretBox(settings.secret_key), settings=settings)
    threading.Timer(0.1, runner.run_now, args=(run_id,)).start()

    # The assertion is that this returns at all. Before the fix it never did.
    _drain(stream)


def test_a_failed_plan_publishes_a_done_event(tmp_path: Path) -> None:
    """The run detail page reloads on `done`. Without one it sits on 'Running'
    while the database already says the run failed."""
    from app.jobs import planner
    from app.jobs.events import broker

    settings = make_settings(tmp_path)
    create_schema(settings)
    session = sessionmaker(bind=create_db_engine(settings))()

    source = Connection(name="s", type=ConnectionType.local, base_path=str(tmp_path))
    dest = Connection(name="d", type=ConnectionType.local, base_path=str(tmp_path))
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Fails",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
    )
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.manual,
        mode=RunMode.dry_run,
        status=RunStatus.running,
    )
    session.add(run)
    session.commit()

    planner._finish_failed(session, run, "The source is not mounted.")

    published = broker.backlog_for(run.id)
    assert any(event.kind == "done" for event in published)
    assert any("not mounted" in event.text for event in published)
    assert run.status == RunStatus.failed
