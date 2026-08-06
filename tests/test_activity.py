"""Live transfer stats and the dashboard activity strip.

The numbers come from rclone's own accounting rather than anything computed
here, so the tests are built on a stats object captured verbatim mid transfer.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.activity import direction_of
from app.config import Settings
from app.db import create_db_engine
from app.engines import parsers
from app.jobs.events import ActivityRecorder, RunBroker, RunEvent
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

# Captured from: rclone copy ... --use-json-log --stats 1s, mid transfer.
SAMPLE = json.loads(
    """
{"bytes":125923328,"checks":0,"deletedDirs":0,"deletes":0,"elapsedTime":2.0004,
 "errors":0,"eta":4,"fatalError":false,"listed":1,"renames":0,"retryError":false,
 "serverSideCopies":0,"speed":63067376.8685269,"totalBytes":419430400,
 "totalChecks":0,"totalTransfers":1,"transferTime":2.0,
 "transferring":[{"bytes":125956096,"dstFs":"/tmp/sp/dst","eta":4,
                  "group":"global_stats","name":"big.bin","percentage":30,
                  "size":419430400,"speed":63096110.7,"speedAvg":63070937.9,
                  "srcFs":"/tmp/sp/src"}]}
"""
)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_stats_are_read_from_rclones_own_accounting() -> None:
    stats = parsers.parse_stats(SAMPLE)

    assert stats.bytes_done == 125923328
    assert stats.total_bytes == 419430400
    assert round(stats.speed) == 63067377
    assert stats.eta_seconds == 4
    assert stats.percentage == 30
    assert stats.total_transfers == 1


def test_the_file_in_flight_is_reported() -> None:
    stats = parsers.parse_stats(SAMPLE)

    assert len(stats.transferring) == 1
    current = stats.transferring[0]
    assert current.name == "big.bin"
    assert current.percentage == 30
    assert current.eta_seconds == 4


def test_a_stats_shape_with_missing_fields_does_not_explode() -> None:
    """This drives a progress display. A later rclone that adds or drops a field
    must not take a running sync with it."""
    stats = parsers.parse_stats({"bytes": 10})

    assert stats.bytes_done == 10
    assert stats.total_bytes == 0
    assert stats.percentage == 0
    assert stats.eta_seconds is None
    assert stats.transferring == ()


def test_percentage_is_safe_before_the_total_is_known() -> None:
    assert parsers.parse_stats({"bytes": 5, "totalBytes": 0}).percentage == 0


# --------------------------------------------------------------------------
# The recorder
# --------------------------------------------------------------------------


def test_session_bytes_count_the_increment_not_the_running_total() -> None:
    """Stats are cumulative per run, so adding the figure each time would count
    the same bytes over and over."""
    recorder = ActivityRecorder()
    recorder.record(1, parsers.parse_stats({"bytes": 100, "speed": 10}))
    recorder.record(1, parsers.parse_stats({"bytes": 250, "speed": 10}))
    recorder.record(1, parsers.parse_stats({"bytes": 400, "speed": 10}))

    total, _peak = recorder.session()
    assert total == 400


def test_two_runs_at_once_sum_into_one_speed() -> None:
    recorder = ActivityRecorder()
    recorder.record(1, parsers.parse_stats({"bytes": 10, "speed": 1000}))
    recorder.record(2, parsers.parse_stats({"bytes": 10, "speed": 2000}))

    _total, peak = recorder.session()
    assert peak == 3000
    assert len(recorder.active()) == 2


def test_a_finished_run_drops_to_zero_rather_than_holding_its_last_speed() -> None:
    """Without the extra sample the chart reads as "still transferring at
    60 MB/s" for several seconds after a sync has stopped."""
    recorder = ActivityRecorder()
    recorder.record(1, parsers.parse_stats({"bytes": 10, "speed": 5000}))
    recorder.forget(1)

    assert recorder.active() == {}
    assert recorder.samples(since_seconds=60)[-1].speed == 0


def test_stats_never_evict_the_log_lines() -> None:
    """The backlog is what someone reads when a run goes wrong. Progress reports
    arrive every few seconds for the length of a sync and would flush it."""
    broker = RunBroker()
    for index in range(10):
        broker.publish(1, RunEvent(kind="line", text=f"line {index}"))

    recorder = ActivityRecorder()
    for index in range(1000):
        recorder.record(1, parsers.parse_stats({"bytes": index, "speed": 1}))

    lines = [event for event in broker.backlog_for(1) if event.kind == "line"]
    assert len(lines) == 10


# --------------------------------------------------------------------------
# Direction, which rclone does not report
# --------------------------------------------------------------------------


def _job(source_type: ConnectionType, dest_type: ConnectionType, **overrides) -> Job:  # noqa: ANN003
    fields: dict = {"name": "j", "direction": Direction.source_to_dest, "filters": {}}
    fields.update(overrides)
    job = Job(**fields)
    job.source_connection = Connection(name="s", type=source_type, base_path="/s", host="h")
    job.dest_connection = Connection(name="d", type=dest_type, base_path="/d", host="h")
    return job


def test_writing_to_a_remote_is_outbound() -> None:
    assert direction_of(_job(ConnectionType.local, ConnectionType.sftp)) == "up"


def test_pulling_from_a_remote_is_inbound() -> None:
    assert direction_of(_job(ConnectionType.smb, ConnectionType.local)) == "down"


def test_direction_follows_the_job_not_the_connection_order() -> None:
    """A dest_to_source job writes to the connection named source."""
    job = _job(ConnectionType.local, ConnectionType.sftp, direction=Direction.dest_to_source)
    assert direction_of(job) == "down"


def test_local_to_local_is_neither() -> None:
    assert direction_of(_job(ConnectionType.local, ConnectionType.local)) == "local"


def test_remote_to_remote_is_not_claimed_as_one_direction() -> None:
    """The bytes arrive and leave again, so calling it one or the other would
    halve or double the figure depending on which was picked."""
    assert direction_of(_job(ConnectionType.sftp, ConnectionType.smb)) == "both"


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


def test_activity_is_quiet_when_nothing_runs(authed_client: TestClient) -> None:
    body = authed_client.get("/api/activity").json()

    assert body["running"] == []
    assert body["total_speed"] == 0
    assert body["up_speed"] == 0


def test_a_running_job_reports_speed_and_the_current_file(
    authed_client: TestClient, settings: Settings
) -> None:
    from app.jobs.events import activity

    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="local-src", type=ConnectionType.local, base_path="/data")
    dest = Connection(name="nas", type=ConnectionType.smb, host="nas", share="Media", base_path="")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Uploading",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
    )
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.manual,
        mode=RunMode.live,
        status=RunStatus.running,
    )
    session.add(run)
    session.commit()

    activity.record(run.id, parsers.parse_stats(SAMPLE))
    try:
        body = authed_client.get("/api/activity").json()
    finally:
        activity.forget(run.id)

    assert len(body["running"]) == 1
    entry = body["running"][0]
    assert entry["job_name"] == "Uploading"
    assert entry["percentage"] == 30
    assert entry["current_file"] == "big.bin"
    assert entry["eta_seconds"] == 4
    assert entry["direction"] == "up"
    # Writing to a remote counts as outbound, and nothing is claimed as inbound.
    assert body["up_speed"] > 0
    assert body["down_speed"] == 0


def test_lifetime_comes_from_the_database_not_memory(
    authed_client: TestClient, settings: Settings
) -> None:
    """The one figure on the strip that should survive a restart."""
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="s", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="d", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(name="Done", source_connection_id=source.id, dest_connection_id=dest.id, filters={})
    session.add(job)
    session.commit()
    session.add(
        JobRun(
            job_id=job.id,
            trigger=RunTrigger.manual,
            mode=RunMode.live,
            status=RunStatus.success,
            bytes_transferred=4096,
        )
    )
    session.commit()

    assert authed_client.get("/api/activity").json()["lifetime_bytes"] == 4096


def test_the_window_selects_how_much_history_is_returned(authed_client: TestClient) -> None:
    body = authed_client.get("/api/activity?window=1h").json()
    assert body["sample_seconds"] == 3600

    # An unknown window falls back rather than failing the whole strip.
    assert authed_client.get("/api/activity?window=nonsense").json()["sample_seconds"] == 60


def test_activity_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/activity").status_code == 401
