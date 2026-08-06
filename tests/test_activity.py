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


# --------------------------------------------------------------------------
# Counting what rclone actually says it did
# --------------------------------------------------------------------------

# Captured from rclone 1.74.4. The message wording is the whole point of these.
COPY_LINES = {
    "new": '{"level":"info","msg":"Copied (new)","size":10,"object":"a.txt"}',
    "replaced": '{"level":"info","msg":"Copied (replaced existing)","size":20,"object":"b.txt"}',
    # A file over --multi-thread-cutoff. This prefix is why a 3 GB transfer was
    # recorded as zero files and zero bytes.
    "multithread": (
        '{"level":"info","msg":"Multi-thread Copied (new)","size":3033782870,"object":"big.mkv"}'
    ),
}
MODTIME_LINE = (
    '{"level":"info","msg":"Updated modification time in destination","size":10,"object":"c.txt"}'
)


def _absorbed(*lines: str) -> parsers.DryRunLog:
    from app.engines import parsers as p
    from app.jobs.runner import _absorb

    observed = p.DryRunLog()
    for line in lines:
        _absorb(observed, line, 1)
    return observed


def test_a_multi_thread_copy_is_a_transfer() -> None:
    """rclone prefixes the message for a file large enough to split, so matching
    the start of the message missed every large transfer."""
    observed = _absorbed(COPY_LINES["multithread"])

    assert [op.path for op in observed.copies] == ["big.mkv"]
    assert observed.copies[0].size == 3033782870


def test_every_copy_wording_counts() -> None:
    observed = _absorbed(*COPY_LINES.values())
    assert len(observed.copies) == 3


def test_a_modification_time_update_is_not_a_transfer() -> None:
    """It moves no data. Counting it inflated files_transferred with a file that
    never crossed the wire."""
    observed = _absorbed(MODTIME_LINE)

    assert observed.copies == []
    assert observed.removals == []


def test_bytes_come_from_rclones_own_total_when_it_reports_one() -> None:
    """Summing per-file sizes counts a whole file even when only part of it
    moved, and depends on every message wording being recognised."""
    from app.engines import parsers as p
    from app.jobs.runner import _bytes_transferred

    observed = p.DryRunLog()
    observed.stats = {"bytes": 999}
    copied = [p.PlannedOperation(path="a", operation=p.SKIPPED_COPY, size=10)]

    assert _bytes_transferred(observed, copied) == 999


def test_bytes_fall_back_to_the_file_sizes_without_stats() -> None:
    from app.engines import parsers as p
    from app.jobs.runner import _bytes_transferred

    copied = [
        p.PlannedOperation(path="a", operation=p.SKIPPED_COPY, size=10),
        p.PlannedOperation(path="b", operation=p.SKIPPED_COPY, size=32),
    ]
    assert _bytes_transferred(p.DryRunLog(), copied) == 42


# --------------------------------------------------------------------------
# The session figures
# --------------------------------------------------------------------------


def test_a_remote_to_remote_transfer_counts_both_ways(
    authed_client: TestClient, settings: Settings
) -> None:
    """The bytes arrive from one endpoint and leave for the other through this
    machine, so it really is receiving and sending. Reporting it as neither left
    the panel reading zero during an obvious transfer."""
    from app.jobs.events import activity

    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="sftp-a", type=ConnectionType.sftp, host="a", base_path="/x")
    dest = Connection(name="smb-b", type=ConnectionType.smb, host="b", share="S", base_path="")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Remote to remote",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
    )
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id, trigger=RunTrigger.manual, mode=RunMode.live, status=RunStatus.running
    )
    session.add(run)
    session.commit()

    activity.record(run.id, parsers.parse_stats({"bytes": 1, "speed": 1000}))
    try:
        body = authed_client.get("/api/activity").json()
    finally:
        activity.forget(run.id)

    assert body["up_speed"] == 1000
    assert body["down_speed"] == 1000
    # The total is the transfer itself, not the two directions added together.
    assert body["total_speed"] == 1000


def test_going_idle_clears_the_session() -> None:
    """Session means this burst of activity, not the lifetime of the process,
    so once nothing is running there is no session to report."""
    recorder = ActivityRecorder()
    recorder.record(1, parsers.parse_stats({"bytes": 500, "speed": 100}))
    assert recorder.session() == (500, 100)

    recorder.forget(1)

    assert recorder.session() == (0, 0.0)


def test_one_run_finishing_does_not_clear_a_session_still_in_progress() -> None:
    """Only the last one out turns the lights off. Otherwise a job finishing
    would zero the totals of another still transferring."""
    recorder = ActivityRecorder()
    recorder.record(1, parsers.parse_stats({"bytes": 500, "speed": 100}))
    recorder.record(2, parsers.parse_stats({"bytes": 300, "speed": 50}))

    recorder.forget(1)

    total, peak = recorder.session()
    assert total == 800
    assert peak == 150

    recorder.forget(2)
    assert recorder.session() == (0, 0.0)


def test_the_session_can_also_be_reset_by_hand() -> None:
    """For clearing the figures part way through a long run."""
    recorder = ActivityRecorder()
    recorder.record(1, parsers.parse_stats({"bytes": 500, "speed": 100}))

    recorder.reset_session()

    assert recorder.session() == (0, 0.0)
    # And the run in flight keeps its baseline rather than counting its own
    # earlier bytes again.
    recorder.record(1, parsers.parse_stats({"bytes": 700, "speed": 100}))
    assert recorder.session()[0] == 200


def test_the_lifetime_total_is_untouched_by_going_idle(
    authed_client: TestClient, settings: Settings
) -> None:
    """It answers "how much has this ever moved", and lives in the database."""
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="s", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="d", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(name="J", source_connection_id=source.id, dest_connection_id=dest.id, filters={})
    session.add(job)
    session.commit()
    session.add(
        JobRun(
            job_id=job.id,
            trigger=RunTrigger.manual,
            mode=RunMode.live,
            status=RunStatus.success,
            bytes_transferred=8192,
        )
    )
    session.commit()

    from app.jobs.events import activity

    activity.record(500, parsers.parse_stats({"bytes": 100, "speed": 10}))
    activity.forget(500)

    body = authed_client.get("/api/activity").json()
    assert body["session_bytes"] == 0
    assert body["lifetime_bytes"] == 8192


def test_resetting_mid_run_counts_only_what_follows(authed_client: TestClient) -> None:
    from app.jobs.events import activity

    activity.record(99, parsers.parse_stats({"bytes": 1000, "speed": 10}))
    try:
        assert authed_client.post("/api/activity/reset-session").status_code == 204
        # The run keeps its baseline, so its earlier bytes are not counted again.
        activity.record(99, parsers.parse_stats({"bytes": 1500, "speed": 10}))
        assert activity.session()[0] == 500
    finally:
        activity.forget(99)
        activity.reset_session()
