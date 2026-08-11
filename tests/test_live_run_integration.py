"""M3's acceptance criteria, against real rclone.

    a live run applies exactly the plan from M2, and a job whose source is
    emptied is refused by the delete brake.

The second half is the important one, and it is why the brake is two mechanisms.
`--max-delete` on its own is an in-flight abort: verified against rclone 1.74.4,
it deletes up to the threshold and then stops. A criterion that says "refused"
needs the run to make no changes at all, which requires a pre-flight veto.

    make test-integration
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.crypto import SecretBox
from app.db import create_db_engine
from app.jobs import planner
from app.jobs.runner import LiveRunner
from app.models import (
    CompareMode,
    Connection,
    ConnectionType,
    DeleteMode,
    Direction,
    Job,
    JobRun,
    RunMode,
    RunStatus,
    RunTrigger,
)
from tests.conftest import create_schema, make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path / "config")
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    return settings, SecretBox(settings.secret_key), factory, factory()


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "identical.txt").write_text("same\n", encoding="utf-8")
    (dst / "identical.txt").write_text("same\n", encoding="utf-8")
    (src / "changed.txt").write_text("the new contents\n", encoding="utf-8")
    (dst / "changed.txt").write_text("old\n", encoding="utf-8")
    (src / "new.txt").write_text("brand new\n", encoding="utf-8")
    (dst / "deleted.txt").write_text("about to go\n", encoding="utf-8")
    return src, dst


def _job(session, src: Path, dst: Path, **overrides) -> Job:  # noqa: ANN003
    source = Connection(name="live-src", type=ConnectionType.local, base_path=str(src))
    dest = Connection(name="live-dst", type=ConnectionType.local, base_path=str(dst))
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "live-job",
        "source_connection_id": source.id,
        "source_path": "",
        "dest_connection_id": dest.id,
        "dest_path": "",
        "direction": Direction.source_to_dest,
        "compare_mode": CompareMode.mtime_size,
        "delete_mode": DeleteMode.delete,
        "max_delete_pct": 20,
        "filters": {},
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _run(env, job: Job) -> JobRun:
    settings, box, factory, session = env
    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    LiveRunner(factory, box=box, settings=settings).run_now(run.id)
    session.expire_all()
    stored = session.get(JobRun, run.id)
    assert stored is not None
    return stored


# --------------------------------------------------------------------------
# Criterion: a live run applies exactly the plan
# --------------------------------------------------------------------------


def test_live_run_applies_exactly_the_plan(tmp_path: Path, env) -> None:
    src, dst = _tree(tmp_path)
    _settings, _box, _factory, session = env
    run = _run(env, _job(session, src, dst))

    assert run.status == RunStatus.success, run.summary
    assert run.summary is not None
    assert run.summary["applied_as_planned"] is True

    # The plan said one new, one updated, one deleted.
    assert run.summary["planned_new"] == 1
    assert run.summary["planned_updated"] == 1
    assert run.summary["planned_deleted"] == 1
    assert run.files_transferred == 2
    assert run.files_deleted == 1

    # And the filesystem agrees.
    assert (dst / "new.txt").read_text(encoding="utf-8") == "brand new\n"
    assert (dst / "changed.txt").read_text(encoding="utf-8") == "the new contents\n"
    assert not (dst / "deleted.txt").exists()
    assert (dst / "identical.txt").read_text(encoding="utf-8") == "same\n"


def test_copy_only_job_never_deletes(tmp_path: Path, env) -> None:
    """Without delete_mode the command is a copy, so extra files simply stay."""
    src, dst = _tree(tmp_path)
    _settings, _box, _factory, session = env
    run = _run(env, _job(session, src, dst, delete_mode=DeleteMode.none))

    assert run.status == RunStatus.success
    assert run.files_deleted == 0
    assert (dst / "deleted.txt").exists(), "a copy must not remove anything"
    assert (dst / "new.txt").exists()


def test_the_run_records_a_redacted_command(tmp_path: Path, env) -> None:
    src, dst = _tree(tmp_path)
    _settings, _box, _factory, session = env
    run = _run(env, _job(session, src, dst))
    assert run.command_redacted is not None
    assert "--max-delete" in run.command_redacted


def test_a_log_file_is_written(tmp_path: Path, env) -> None:
    """SPEC section 16: per-run logs under /config/logs/<job-id>/<run-id>.log."""
    src, dst = _tree(tmp_path)
    _settings, _box, _factory, session = env
    run = _run(env, _job(session, src, dst))
    assert run.log_path is not None
    log = Path(run.log_path)
    assert log.is_file()
    assert log.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------
# Criterion: an emptied source is refused by the delete brake
# --------------------------------------------------------------------------


def test_emptied_source_is_refused_and_changes_nothing(tmp_path: Path, env) -> None:
    """The heart of M3, and the reason the brake needs a pre-flight veto.

    --max-delete alone would delete up to the threshold and then abort. Refusal
    means nothing is removed at all.
    """
    _settings, _box, _factory, session = env
    src = tmp_path / "empty-src"
    dst = tmp_path / "full-dst"
    src.mkdir()
    dst.mkdir()
    for index in range(10):
        (dst / f"file{index}.txt").write_text("valuable\n", encoding="utf-8")

    run = _run(env, _job(session, src, dst, max_delete_pct=20))

    assert run.status == RunStatus.failed
    assert run.summary is not None
    message = run.summary["error"]
    assert "delete brake" in message
    assert "Nothing was changed" in message

    # Every single file survives. Not "most of them".
    survivors = sorted(path.name for path in dst.iterdir())
    assert len(survivors) == 10
    assert run.files_deleted == 0


def test_deletions_within_the_brake_are_allowed(tmp_path: Path, env) -> None:
    """The brake must not block ordinary work: 1 of 10 against a 20% limit."""
    _settings, _box, _factory, session = env
    src = tmp_path / "s"
    dst = tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    for index in range(9):
        (src / f"keep{index}.txt").write_text("keep\n", encoding="utf-8")
        (dst / f"keep{index}.txt").write_text("keep\n", encoding="utf-8")
    (dst / "gone.txt").write_text("gone\n", encoding="utf-8")

    run = _run(env, _job(session, src, dst, max_delete_pct=20))
    assert run.status == RunStatus.success, run.summary
    assert run.files_deleted == 1
    assert not (dst / "gone.txt").exists()


def test_missing_sentinel_refuses_before_touching_anything(tmp_path: Path, env) -> None:
    """SPEC 6.4. A stale mount presents as an empty directory, which is
    indistinguishable from 'delete everything'. Re-checked at run time rather
    than trusted from the last connection test."""
    _settings, _box, _factory, session = env
    src = tmp_path / "stale-mount"
    dst = tmp_path / "dest"
    src.mkdir()
    dst.mkdir()
    (dst / "precious.txt").write_text("do not delete\n", encoding="utf-8")

    job = _job(session, src, dst)
    job.source_connection.sentinel_file = ".hivesync-mounted"
    session.commit()

    run = _run(env, job)
    assert run.status == RunStatus.failed
    assert "sentinel" in run.summary["error"]
    assert (dst / "precious.txt").exists()


def test_present_sentinel_allows_the_run(tmp_path: Path, env) -> None:
    _settings, _box, _factory, session = env
    src = tmp_path / "mounted"
    dst = tmp_path / "dest2"
    src.mkdir()
    dst.mkdir()
    (src / ".hivesync-mounted").write_text("", encoding="utf-8")
    (src / "data.txt").write_text("real\n", encoding="utf-8")

    job = _job(session, src, dst, max_delete_pct=100)
    job.source_connection.sentinel_file = ".hivesync-mounted"
    session.commit()

    run = _run(env, job)
    assert run.status == RunStatus.success, run.summary
    assert (dst / "data.txt").exists()


def test_brake_flag_is_present_even_on_a_permitted_run(tmp_path: Path, env) -> None:
    """Invariant 7: no live sync runs without --max-delete, whatever the outcome."""
    src, dst = _tree(tmp_path)
    _settings, _box, _factory, session = env
    run = _run(env, _job(session, src, dst, max_delete_pct=100))
    assert run.command_redacted is not None
    assert "--max-delete" in run.command_redacted


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


def test_cancel_stops_the_run_and_records_what_was_done(tmp_path: Path, env) -> None:
    """A cancelled sync reports the work it completed, not nothing. The next
    run's brake reads the resulting state, so pretending otherwise would mislead
    the thing protecting the destination."""
    settings, box, factory, session = env
    src = tmp_path / "big-src"
    dst = tmp_path / "big-dst"
    src.mkdir()
    dst.mkdir()
    # Large enough, and rate limited, that the transfer is still going when the
    # cancel lands.
    for index in range(4):
        (src / f"big{index}.bin").write_bytes(b"x" * 8_000_000)

    job = _job(session, src, dst, bwlimit="2M", max_delete_pct=100)
    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    runner = LiveRunner(factory, box=box, settings=settings)

    def cancel_shortly() -> None:
        time.sleep(6)
        runner.cancel(run.id)

    timer = threading.Thread(target=cancel_shortly, daemon=True)
    timer.start()
    runner.run_now(run.id)
    timer.join(timeout=5)

    session.expire_all()
    stored = session.get(JobRun, run.id)
    assert stored is not None
    assert stored.status == RunStatus.cancelled, stored.summary

    # Verified against rclone: SIGTERM removes the partial file it was writing,
    # so nothing half-written is left under a final name.
    partials = list(dst.glob("*.partial"))
    assert not partials, f"a cancelled transfer left partial files: {partials}"
    for path in dst.iterdir():
        assert path.stat().st_size == 8_000_000, f"{path.name} is truncated"


def test_cancelling_a_finished_run_is_a_no_op(tmp_path: Path, env) -> None:
    settings, box, factory, session = env
    src, dst = _tree(tmp_path)
    run = _run(env, _job(session, src, dst))
    assert run.status == RunStatus.success
    assert LiveRunner(factory, box=box, settings=settings).cancel(run.id) is False


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


def test_only_one_run_per_job_at_a_time(tmp_path: Path, env) -> None:
    """SPEC 6.2, enforced by the database rather than by a prior SELECT."""
    src, dst = _tree(tmp_path)
    _settings, _box, _factory, session = env
    job = _job(session, src, dst)
    planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    with pytest.raises(planner.RunConflict):
        planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)


# --------------------------------------------------------------------------
# Approving a refused set of deletions
#
# The escape hatch on the delete brake, which is the most consequential control
# in this program. Asserted on filesystem state, never on messages.
# --------------------------------------------------------------------------


def _approved(env, job: Job, deletions: int) -> JobRun:
    """A live run carrying an operator's approval of a specific count."""
    settings, box, factory, session = env
    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    run.forced_max_delete = deletions
    session.commit()
    LiveRunner(factory, box=box, settings=settings).run_now(run.id)
    session.expire_all()
    stored = session.get(JobRun, run.id)
    assert stored is not None
    return stored


def _doomed(tmp_path: Path, count: int) -> tuple[Path, Path]:
    """A destination holding files the source no longer has."""
    src = tmp_path / "src2"
    dst = tmp_path / "dst2"
    src.mkdir()
    dst.mkdir()
    (src / "keep.txt").write_text("kept\n", encoding="utf-8")
    (dst / "keep.txt").write_text("kept\n", encoding="utf-8")
    for index in range(count):
        (dst / f"doomed{index}.txt").write_text("about to go\n", encoding="utf-8")
    return src, dst


def test_a_refused_run_records_what_it_would_have_deleted(tmp_path: Path, env) -> None:
    """An error banner with no file list is the one thing an operator cannot act
    on, and it was all a refusal produced."""
    from app.models import JobRunChange

    _settings, _box, _factory, session = env
    src, dst = _doomed(tmp_path, 3)

    run = _run(env, _job(session, src, dst, max_delete_pct=1))

    assert run.status == RunStatus.failed
    assert run.summary["refused_deletions"] == 3, run.summary
    listed = {c.path for c in session.query(JobRunChange).filter_by(run_id=run.id).all()}
    assert {"doomed0.txt", "doomed1.txt", "doomed2.txt"} <= listed
    assert len(sorted(dst.iterdir())) == 4


def test_approving_the_deletions_lets_exactly_those_through(tmp_path: Path, env) -> None:
    _settings, _box, _factory, session = env
    src, dst = _doomed(tmp_path, 3)
    job = _job(session, src, dst, max_delete_pct=1)

    refused = _run(env, job)
    assert refused.status == RunStatus.failed

    approved = _approved(env, job, refused.summary["refused_deletions"])

    assert approved.status == RunStatus.success, approved.summary
    assert sorted(path.name for path in dst.iterdir()) == ["keep.txt"]


def test_an_approval_does_not_authorise_a_larger_set(tmp_path: Path, env) -> None:
    """Why the approval is a count and not a switch. Agreeing to three deletions
    must not authorise six if more disappears in between."""
    _settings, _box, _factory, session = env
    src, dst = _doomed(tmp_path, 3)
    job = _job(session, src, dst, max_delete_pct=1)

    refused = _run(env, job)
    assert refused.summary["refused_deletions"] == 3

    for index in range(3, 6):
        (dst / f"doomed{index}.txt").write_text("also doomed\n", encoding="utf-8")

    run = _approved(env, job, 3)

    assert run.status == RunStatus.failed, run.summary
    # Nothing removed at all: the veto fires before rclone is invoked.
    assert len(sorted(dst.iterdir())) == 7
    assert run.files_deleted == 0


def test_an_approval_is_for_one_run_only(tmp_path: Path, env) -> None:
    """The job's own brake is untouched, so the next scheduled run is refused
    again rather than inheriting the decision."""
    _settings, _box, _factory, session = env
    src, dst = _doomed(tmp_path, 3)
    job = _job(session, src, dst, max_delete_pct=1)

    assert _approved(env, job, 3).status == RunStatus.success
    session.refresh(job)
    assert job.max_delete_pct == 1

    # A fresh set of deletions, and no approval this time.
    for index in range(3):
        (dst / f"later{index}.txt").write_text("new arrival\n", encoding="utf-8")

    run = _run(env, job)

    assert run.status == RunStatus.failed
    assert len(sorted(dst.iterdir())) == 4


# --------------------------------------------------------------------------
# The quiet period: a file still being written
# --------------------------------------------------------------------------


def test_a_file_still_being_written_is_left_alone(tmp_path: Path, env) -> None:
    """The reported case: a download client is writing into the source while a
    schedule fires. Half a file copied unattended is worse than a file collected
    on the next run.

    Asserted on filesystem state against real rclone, because --min-age is
    rclone's own comparison and a unit test would only prove we build the flag.
    """
    _settings, _box, _factory, session = env
    src = tmp_path / "src3"
    dst = tmp_path / "dst3"
    src.mkdir()
    dst.mkdir()

    settled = src / "settled.txt"
    settled.write_text("finished ages ago\n", encoding="utf-8")
    # Old enough to be past the quiet period.
    old = time.time() - 600
    os.utime(settled, (old, old))

    # Written just now, as a download in progress would be.
    (src / "downloading.part").write_text("half a file\n", encoding="utf-8")

    run = _run(env, _job(session, src, dst, continuous=False, quiet_period_seconds=60))

    assert run.status == RunStatus.success, run.summary
    assert (dst / "settled.txt").exists(), "a settled file should still be copied"
    assert not (dst / "downloading.part").exists(), (
        "a file modified within the quiet period was copied while it was still being written"
    )


def test_the_file_arrives_once_it_has_settled(tmp_path: Path, env) -> None:
    """Skipped, not dropped. The next run collects it."""
    _settings, _box, _factory, session = env
    src = tmp_path / "src4"
    dst = tmp_path / "dst4"
    src.mkdir()
    dst.mkdir()
    target = src / "downloading.part"
    target.write_text("complete now\n", encoding="utf-8")

    job = _job(session, src, dst, continuous=False, quiet_period_seconds=60)
    assert _run(env, job).status == RunStatus.success
    assert not (dst / "downloading.part").exists()

    # Time passes and the file stops changing.
    settled = time.time() - 600
    os.utime(target, (settled, settled))

    assert _run(env, job).status == RunStatus.success
    assert (dst / "downloading.part").read_text(encoding="utf-8") == "complete now\n"


def test_a_dry_run_and_a_live_run_see_the_same_files(tmp_path: Path, env) -> None:
    """Without --min-age in the planning phases a preview lists a file the run
    then skips, and the two disagree about the same tree."""
    _settings, _box, _factory, session = env
    src = tmp_path / "src5"
    dst = tmp_path / "dst5"
    src.mkdir()
    dst.mkdir()
    (src / "downloading.part").write_text("half\n", encoding="utf-8")

    job = _job(session, src, dst, continuous=False, quiet_period_seconds=60)
    planned = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)
    settings, box, factory, _session = env
    planner.PlanRunner(factory, box=box, settings=settings).run_now(planned.id)
    session.expire_all()
    preview = session.get(JobRun, planned.id)

    assert preview is not None
    assert preview.status == RunStatus.success, preview.summary
    assert preview.summary["new"] == 0, "the preview offered to copy a file the run would skip"


def test_a_transferred_file_records_how_fast_it_moved(tmp_path: Path, env) -> None:
    """Per-file speed appears only in the `transferring` array of the stats
    events, while the file is in flight, and was thrown away when the run ended.

    The transfer has to outlive a stats tick, and the interval is 5 seconds, so
    a 64MB local copy is measured at exactly nothing: it starts and finishes
    between two ticks and never appears in `transferring` at all. Throttled here
    so it spans one, which is also why this is an integration test.
    """
    from app.models import JobRunChange

    _settings, _box, _factory, session = env
    src = tmp_path / "psrc"
    dst = tmp_path / "pdst"
    src.mkdir()
    dst.mkdir()
    (src / "big.bin").write_bytes(os.urandom(48 * 1024 * 1024))

    run = _run(env, _job(session, src, dst, bwlimit="4M"))

    assert run.status == RunStatus.success, run.summary
    rows = session.query(JobRunChange).filter_by(run_id=run.id).all()
    big = next(row for row in rows if row.path == "big.bin")
    assert big.peak_speed_bps is not None, "no per-file speed was recorded"
    assert big.peak_speed_bps > 0


def test_a_deletion_records_no_speed(tmp_path: Path, env) -> None:
    """Nothing was transferred, so there is nothing to measure. NULL rather than
    zero, which would claim a measurement that was never taken."""
    from app.models import JobRunChange

    _settings, _box, _factory, session = env
    src = tmp_path / "dsrc"
    dst = tmp_path / "ddst"
    src.mkdir()
    dst.mkdir()
    (src / "keep.txt").write_text("kept\n", encoding="utf-8")
    (dst / "keep.txt").write_text("kept\n", encoding="utf-8")
    (dst / "gone.txt").write_text("removed\n", encoding="utf-8")

    run = _run(env, _job(session, src, dst))

    assert run.status == RunStatus.success, run.summary
    rows = session.query(JobRunChange).filter_by(run_id=run.id).all()
    gone = next(row for row in rows if row.path == "gone.txt")
    assert gone.peak_speed_bps is None
