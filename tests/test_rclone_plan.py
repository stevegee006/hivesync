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


def test_bidirectional_is_refused_with_an_explanation() -> None:
    engine = rclone.RcloneEngine()
    with pytest.raises(EngineError, match="Bidirectional"):
        engine.plan(_job(direction=Direction.bidirectional), box=None, settings=None)  # type: ignore[arg-type]


def test_execute_refuses_rather_than_pretending() -> None:
    engine = rclone.RcloneEngine()
    with pytest.raises(EngineError, match="not implemented"):
        engine.execute(_job(), box=None, settings=None)  # type: ignore[arg-type]
