"""Flag construction for a plan.

These do not run rclone. They assert on the argv the engine would build, which is
where a mistake becomes a wrong plan or, later, a wrong deletion.
"""

from __future__ import annotations

import pytest

from app.engines import rclone
from app.engines.base import EngineError
from app.models import (
    CompareMode,
    Connection,
    ConnectionType,
    DeleteMode,
    Direction,
    Engine,
    FilterPreset,
    Job,
    utcnow,
)

HASHED_PROBE = {"Precision": 1, "Hashes": ["md5", "sha1"], "Features": {"Move": True}}
HASHLESS_PROBE = {"Precision": 1, "Hashes": [], "Features": {"Move": True}}


def _connection(name: str, probe: dict | None = HASHED_PROBE) -> Connection:
    connection = Connection(
        name=name, type=ConnectionType.sftp, host="example.test", base_path="/srv"
    )
    if probe is not None:
        connection.capabilities = probe
        connection.capabilities_probed_at = utcnow()
    return connection


def _job(**overrides: object) -> Job:
    job = Job(
        name="nightly",
        source_path="www",
        dest_path="Media/www",
        engine=Engine.rclone,
        direction=Direction.source_to_dest,
        compare_mode=CompareMode.mtime_size,
        modify_window="1s",
        max_delete_pct=20,
        filters={},
    )
    job.source_connection = _connection("prod-sftp")
    job.dest_connection = _connection("synology")
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


# --------------------------------------------------------------------------
# Comparison mode
# --------------------------------------------------------------------------


def test_mtime_size_adds_only_the_modify_window() -> None:
    """The default comparison needs no flag. modify_window is always passed,
    because a NAS clock drift otherwise re-transfers unchanged files forever."""
    args = rclone.comparison_args(_job())
    assert args == ["--modify-window", "1s"]


def test_size_only_passes_the_flag() -> None:
    args = rclone.comparison_args(_job(compare_mode=CompareMode.size_only))
    assert "--size-only" in args


def test_checksum_allowed_when_a_hash_is_shared() -> None:
    args = rclone.comparison_args(_job(compare_mode=CompareMode.checksum))
    assert "--checksum" in args


def test_checksum_refused_when_no_hash_is_shared() -> None:
    """Second line of defence behind the job editor. Asking for --checksum
    against a hash-less backend compares nothing useful."""
    job = _job(compare_mode=CompareMode.checksum)
    job.dest_connection = _connection("synology-smb", HASHLESS_PROBE)
    with pytest.raises(EngineError, match="share no hash type"):
        rclone.comparison_args(job)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_job_filters_become_flags() -> None:
    job = _job(filters={"include": ["*.jpg"], "exclude": ["*.tmp"], "min_size": "1k"})
    args = rclone.filter_args(job)
    assert args[:2] == ["--include", "*.jpg"]
    assert "--exclude" in args
    assert "*.tmp" in args
    assert args[-2:] == ["--min-size", "1k"]


def test_preset_rules_are_included() -> None:
    job = _job()
    job.filter_presets = [
        FilterPreset(name="Synology / DSM", builtin=True, rules={"exclude": ["**/@eaDir/**"]})
    ]
    assert "**/@eaDir/**" in rclone.filter_args(job)


def test_job_excludes_come_after_preset_excludes() -> None:
    """rclone applies the first matching rule, so a job must be able to override
    what a preset excluded."""
    job = _job(filters={"exclude": ["job-rule"]})
    job.filter_presets = [FilterPreset(name="p", builtin=True, rules={"exclude": ["preset-rule"]})]
    args = rclone.filter_args(job)
    assert args.index("preset-rule") < args.index("job-rule")


def test_blank_filter_rules_are_dropped() -> None:
    job = _job(filters={"exclude": ["", "   ", "real"]})
    assert rclone.filter_args(job).count("--exclude") == 1


def test_no_filters_produces_no_flags() -> None:
    assert rclone.filter_args(_job()) == []


# --------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------


def test_performance_flags_are_optional() -> None:
    assert rclone.performance_args(_job()) == []


def test_performance_flags_when_set() -> None:
    args = rclone.performance_args(_job(transfers=8, checkers=16, bwlimit="10M"))
    assert args == ["--transfers", "8", "--checkers", "16", "--bwlimit", "10M"]


# --------------------------------------------------------------------------
# The delete brake
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "dest_count", "expected"),
    [
        (20, 100, 20),
        (20, 10, 2),
        (20, 0, 1),  # nothing on the destination: still allow one
        (0, 100, 1),  # a zero percent brake still permits one deliberate deletion
        (100, 50, 50),
        (20, 7, 2),  # rounds up, so a small tree is not blocked by rounding
    ],
)
def test_percentage_resolves_to_a_count(pct: int, dest_count: int, expected: int) -> None:
    """SPEC 6.4 and invariant 7 both describe --max-delete as a percentage.
    Verified against rclone 1.74.4: it takes an int count and no percentage flag
    exists, so the conversion has to happen here."""
    assert rclone.resolve_max_delete(pct, dest_count) == expected


# --------------------------------------------------------------------------
# Direction
# --------------------------------------------------------------------------


def test_direction_swaps_both_connections_and_paths() -> None:
    """The subpaths must swap with the connections. Reading from dest_connection
    while still applying source_path would plan against the wrong tree."""
    job = _job(direction=Direction.dest_to_source)
    read, write = rclone._endpoints_for(job)
    read_path, write_path = rclone._paths_for(job)
    assert read.name == "synology"
    assert write.name == "prod-sftp"
    assert read_path == "Media/www"
    assert write_path == "www"


def test_forward_direction_is_unswapped() -> None:
    job = _job()
    read, write = rclone._endpoints_for(job)
    assert (read.name, write.name) == ("prod-sftp", "synology")
    assert rclone._paths_for(job) == ("www", "Media/www")


def test_side_recorded_matches_the_side_written() -> None:
    from app.models import ChangeSide

    assert rclone.side_for(_job()) == ChangeSide.dest
    assert rclone.side_for(_job(direction=Direction.dest_to_source)) == ChangeSide.source


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_the_one_way_engine_refuses_a_bidirectional_job() -> None:
    """A guard, not a limitation. Bidirectional jobs are planned by
    BisyncEngine, which `planner.engine_for` selects; reaching this means
    something called the wrong engine directly.

    It used to be the limitation, and the message promised a milestone that had
    already shipped, so a dry run of a bidirectional job failed telling the
    reader to wait for a feature they already had."""
    engine = rclone.RcloneEngine()
    with pytest.raises(EngineError, match="one way jobs only"):
        engine.plan(_job(direction=Direction.bidirectional), box=None, settings=None)  # type: ignore[arg-type]


def test_the_planner_routes_a_bidirectional_job_to_bisync() -> None:
    """The actual fix. Without this branch every bidirectional dry run hit the
    one way engine and was refused."""
    from app.engines.bisync import BisyncEngine
    from app.jobs.planner import engine_for

    assert isinstance(engine_for(_job(direction=Direction.bidirectional)), BisyncEngine)
    assert isinstance(engine_for(_job(direction=Direction.source_to_dest)), rclone.RcloneEngine)


def test_execute_directs_callers_to_the_runner() -> None:
    """A live run has to be streamed and cancellable, which this signature cannot
    express, so the runner drives the process instead."""
    engine = rclone.RcloneEngine()
    with pytest.raises(EngineError, match="job runner"):
        engine.execute(_job(), box=None, settings=None)  # type: ignore[arg-type]


def test_sync_command_always_carries_the_delete_brake() -> None:
    """Invariant 7 has no exceptions, and this is the only place a live sync
    command is built."""
    from app.crypto import Redactor
    from app.engines.rcloneconf import ALIAS_DEST, ALIAS_SOURCE, Prepared

    prepared = Prepared(endpoints={}, env={}, base_args=["--config", ""], redactor=Redactor([]))
    argv = rclone.build_sync_command(
        _job(delete_mode=DeleteMode.delete), prepared, "src:", "dst:", max_delete=7
    )
    assert "--max-delete" in argv
    assert argv[argv.index("--max-delete") + 1] == "7"
    assert "sync" in argv
    _ = (ALIAS_DEST, ALIAS_SOURCE)


def test_copy_is_used_when_deletion_is_off() -> None:
    """Without delete_mode the command must not be able to remove anything, so
    it is a copy rather than a sync with an unused brake."""
    from app.crypto import Redactor
    from app.engines.rcloneconf import Prepared

    prepared = Prepared(endpoints={}, env={}, base_args=["--config", ""], redactor=Redactor([]))
    argv = rclone.build_sync_command(
        _job(delete_mode=DeleteMode.none), prepared, "src:", "dst:", max_delete=7
    )
    assert "copy" in argv
    assert "sync" not in argv
    # Still passed, so the invariant holds regardless of the operation.
    assert "--max-delete" in argv
