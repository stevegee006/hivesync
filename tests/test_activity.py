"""Live transfer stats and the dashboard activity strip.

The numbers come from rclone's own accounting rather than anything computed
here, so the tests are built on a stats object captured verbatim mid transfer.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.activity import direction_of
from app.config import Settings
from app.db import create_db_engine
from app.engines import parsers
from app.jobs.events import ActivityRecorder, RunBroker, RunEvent
from app.models import (
    ChangeAction,
    Connection,
    ConnectionType,
    Direction,
    Job,
    JobRun,
    JobRunChange,
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


# --------------------------------------------------------------------------
# Per-file progress on the run page
# --------------------------------------------------------------------------

# Two files in flight at once, captured from rclone 1.74.4 with --transfers 4.
# The per-file `eta` is null here, which is what rclone reports until it has
# enough history to estimate one. A row has to render without it.
TWO_IN_FLIGHT = json.loads(
    """
{"bytes":21093152,"elapsedTime":1.0,"errors":0,"eta":null,"speed":41986000.0,
 "totalBytes":300000000,"totalTransfers":2,"transfers":0,
 "transferring":[{"bytes":10874880,"dstFs":"/tmp/pb","eta":null,
                  "group":"global_stats","name":"big.bin","percentage":5,
                  "size":200000000,"speed":21821871.4,"speedAvg":0,
                  "srcFs":"/tmp/pa"},
                 {"bytes":10219520,"dstFs":"/tmp/pb","eta":null,
                  "group":"global_stats","name":"mid.bin","percentage":10,
                  "size":100000000,"speed":20523095.3,"speedAvg":0,
                  "srcFs":"/tmp/pa"}]}
"""
)


_run_ids = itertools.count(987654)


def _published_stats(payload: dict[str, object]) -> dict[str, object]:
    """Absorb one stats line and return the data of the event it published.

    A fresh run id each time: the broker remembers finished runs so that a
    browser arriving late is not left hanging, which means a reused id is
    closed before the second test subscribes.
    """
    from app.jobs import events
    from app.jobs.runner import _absorb

    run_id = next(_run_ids)
    try:
        # rclone carries the stats object inside an ordinary log record.
        line = json.dumps({"level": "info", "msg": "", "stats": payload})
        _absorb(parsers.DryRunLog(), line, run_id)
        # Subscribing afterwards on purpose: a stats event has to survive until
        # a browser connects, which is the whole point of replaying the latest.
        for event in events.broker.subscribe(run_id):
            if event.kind == "stats":
                return event.data
            break
    finally:
        events.broker.finish(run_id)
    raise AssertionError("no stats event was published")


def test_the_run_stream_carries_each_file_in_flight() -> None:
    """The page cannot show per-file progress that the event does not carry, and
    for a while the event dropped the whole `transferring` array."""
    data = _published_stats(TWO_IN_FLIGHT)

    files = data["files"]
    assert [f["name"] for f in files] == ["big.bin", "mid.bin"]
    assert [f["percentage"] for f in files] == [5, 10]
    assert [f["size"] for f in files] == [200000000, 100000000]
    assert round(files[0]["speed"]) == 21821871


def test_a_file_with_no_estimate_yet_still_reports_progress() -> None:
    """rclone reports a null per-file eta until it has history. Dropping the row
    until an ETA exists would leave the panel empty for the first few seconds of
    every transfer."""
    data = _published_stats(TWO_IN_FLIGHT)

    assert all(f["eta"] is None for f in data["files"])
    assert all(f["bytes"] > 0 for f in data["files"])


def test_a_transfer_with_nothing_in_flight_publishes_an_empty_list() -> None:
    """Not a missing key: the page reads `files` on every event."""
    payload = dict(TWO_IN_FLIGHT)
    payload.pop("transferring")

    data = _published_stats(payload)

    assert data["files"] == []
    assert data["total_bytes"] == 300000000


def test_stats_events_never_evict_the_log_backlog() -> None:
    """They used to. Every event went into one 200 entry list, so at --stats 5s
    a twenty minute sync pushed every log line out of it and a browser opening
    the run page mid transfer saw progress bars above an empty log."""
    from app.jobs.events import RunBroker

    broker = RunBroker()
    broker.publish(1, RunEvent(kind="line", text="the line that matters"))
    for _ in range(500):
        broker.publish(1, RunEvent(kind="stats", data={"bytes": 1}))

    replayed = list(broker.backlog_for(1))

    assert [event.text for event in replayed] == ["the line that matters"]


def test_a_page_opened_mid_transfer_gets_the_current_progress() -> None:
    """Otherwise the panel stays blank until the next stats tick, which is up to
    the whole --stats interval away."""
    from app.jobs.events import RunBroker

    broker = RunBroker()
    broker.publish(2, RunEvent(kind="line", text="starting"))
    broker.publish(2, RunEvent(kind="stats", data={"percentage": 40}))
    broker.publish(2, RunEvent(kind="stats", data={"percentage": 70}))

    seen = []
    for event in broker.subscribe(2):
        seen.append(event)
        if event.kind == "stats":
            break

    # The latest, not every one of them: superseded progress is not history.
    assert [event.kind for event in seen] == ["line", "stats"]
    assert seen[-1].data == {"percentage": 70}


# --------------------------------------------------------------------------
# What a live run reports
# --------------------------------------------------------------------------

# Verified against rclone 1.74.4, which is the only place the two can be told
# apart: a dry run reports skipped:"copy" for both.
LIVE_COPY_LINES = {
    "new": '{"level":"info","msg":"Copied (new)","size":10,"object":"new.txt"}',
    "replaced": (
        '{"level":"info","msg":"Copied (replaced existing)","size":20,"object":"old.txt"}'
    ),
    "big": '{"level":"info","msg":"Multi-thread Copied (new)","size":3033782870,"object":"b.mkv"}',
}


def test_a_live_run_can_tell_a_new_file_from_an_updated_one() -> None:
    """The run page showed New 0 and Updated 0 on every live run, however many
    files it copied, because nothing recorded which was which."""
    observed = _absorbed(*LIVE_COPY_LINES.values())

    assert [op.path for op in observed.created] == ["new.txt", "b.mkv"]
    assert [op.path for op in observed.replacements] == ["old.txt"]
    # And they are still all copies.
    assert len(observed.copies) == 3


def test_a_dry_run_copy_claims_neither() -> None:
    """A dry run reports skipped:"copy" for both, so the planner's presence pass
    is what tells them apart. Guessing here would be worse than not knowing."""
    from app.engines import parsers as p

    operation = p.PlannedOperation(path="a.txt", operation=p.SKIPPED_COPY)

    assert operation.replaced is None
    log = p.DryRunLog(operations=[operation])
    assert log.created == []
    assert log.replacements == []


# --------------------------------------------------------------------------
# The change table and the summary cards, which must agree
# --------------------------------------------------------------------------


def _recorded(settings, *lines: str, planned: list[str]) -> tuple[dict, dict[str, str]]:
    """Record a finished one way run and return its summary and its change rows.

    `planned` is what the pre-flight predicted, by path. It exists to prove the
    plan does not decide the label.
    """
    from conftest import create_schema

    from app.engines.base import Plan, PlannedChange
    from app.jobs.runner import _record

    create_schema(settings)
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="UltraCC", type=ConnectionType.local, base_path="/src")
    dest = Connection(name="Synology", type=ConnectionType.local, base_path="/dst")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Movies",
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

    plan = Plan(
        changes=[PlannedChange(action=ChangeAction.new, path=p) for p in planned],
        unchanged_count=30,
    )
    _record(
        session=session,
        run=run,
        job=job,
        plan=plan,
        observed=_absorbed(*lines),
        exit_code=0,
        threshold=6,
        log_path=Path("/config/logs/x.log"),
    )
    session.commit()
    rows = {c.path: c.action.value for c in session.query(JobRunChange).all()}
    return dict(run.summary), rows


def test_a_new_file_the_plan_predicted_is_recorded_as_new(settings) -> None:
    """The reported bug, at the size it was found.

    A 9.3 GB file that had never been on the destination was listed as
    "updated". The label came from whether the plan had mentioned the path, and
    `plan.changes` holds every planned change of any kind, so a correctly
    predicted new file read as an update. Only a file that appeared after
    planning could come out new, which is backwards.
    """
    summary, rows = _recorded(
        settings,
        COPY_LINES["multithread"],
        planned=["big.mkv"],
    )

    assert rows == {"big.mkv": "new"}
    assert summary["new"] == 1
    assert summary["updated"] == 0


def test_the_change_rows_agree_with_the_summary_cards(settings) -> None:
    """One run, two writers, one screen.

    The cards counted through `observed.created`, from rclone's copy wording,
    while the rows were derived from the plan. They disagreed in front of the
    reader: New 1 above a table whose only row said updated. Anything that can
    label a change has to answer from the same source.
    """
    summary, rows = _recorded(
        settings,
        COPY_LINES["new"],
        COPY_LINES["replaced"],
        COPY_LINES["multithread"],
        # Everything was predicted, which is the normal case and the one that
        # used to make every row say updated.
        planned=["a.txt", "b.txt", "big.mkv"],
    )

    by_action = Counter(rows.values())
    assert by_action["new"] == summary["new"] == 2
    assert by_action["updated"] == summary["updated"] == 1


def test_a_file_that_appeared_after_planning_is_still_labelled_by_rclone(settings) -> None:
    """The old rule called an unplanned path new. rclone says it replaced
    something that was already there, and rclone is the one that looked."""
    summary, rows = _recorded(settings, COPY_LINES["replaced"], planned=[])

    assert rows == {"b.txt": "updated"}
    assert summary["updated"] == 1
