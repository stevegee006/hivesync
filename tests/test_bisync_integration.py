"""M5's acceptance criteria, against real rclone.

    files created independently on both sides converge, an edit-edit conflict
    produces a conflict-loser copy rather than data loss, and a wiped workdir
    surfaces the resync prompt instead of failing silently.

SPEC section 10 calls this the place naive implementations lose data, so these
assert on the contents of both trees rather than on exit codes.

    make test-integration
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.crypto import SecretBox
from app.db import create_db_engine
from app.engines import bisync
from app.jobs import planner
from app.jobs.runner import LiveRunner
from app.models import (
    ConflictResolve,
    Connection,
    ConnectionType,
    DeleteMode,
    Direction,
    Job,
    JobRun,
    JobRunChange,
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


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    path1 = tmp_path / "p1"
    path2 = tmp_path / "p2"
    path1.mkdir()
    path2.mkdir()
    return path1, path2


def _job(session, p1: Path, p2: Path, **overrides) -> Job:  # noqa: ANN003
    source = Connection(name="bi-1", type=ConnectionType.local, base_path=str(p1))
    dest = Connection(name="bi-2", type=ConnectionType.local, base_path=str(p2))
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "two-way",
        "source_connection_id": source.id,
        "dest_connection_id": dest.id,
        "source_path": "",
        "dest_path": "",
        "direction": Direction.bidirectional,
        "delete_mode": DeleteMode.none,
        "conflict_resolve": ConflictResolve.newer,
        "max_delete_pct": 50,
        "filters": {},
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _run(env, job: Job, *, resync: bool = False) -> JobRun:
    settings, box, factory, session = env
    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.live)
    if resync:
        run.is_resync = True
        session.commit()
    LiveRunner(factory, box=box, settings=settings).run_now(run.id)
    session.expire_all()
    stored = session.get(JobRun, run.id)
    assert stored is not None
    return stored


# --------------------------------------------------------------------------
# Resync gating
# --------------------------------------------------------------------------


def test_first_run_is_refused_until_an_explicit_first_sync(tmp_path: Path, env) -> None:
    """Invariant: bisync never auto-resyncs. A resync makes one side match the
    other, so it must be a decision rather than a side effect."""
    p1, p2 = _pair(tmp_path)
    (p1 / "a.txt").write_text("hello\n", encoding="utf-8")
    _settings, _box, _factory, session = env

    run = _run(env, _job(session, p1, p2))

    assert run.status == RunStatus.failed
    assert "first sync" in run.summary["error"].lower()
    # Nothing was copied, in either direction.
    assert list(p2.iterdir()) == []


def test_resync_initialises_and_then_normal_runs_work(tmp_path: Path, env) -> None:
    p1, p2 = _pair(tmp_path)
    (p1 / "a.txt").write_text("hello\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2)

    first = _run(env, job, resync=True)
    assert first.status == RunStatus.success, first.summary
    assert first.is_resync is True
    session.refresh(job)
    assert job.bisync_initialized is True
    assert (p2 / "a.txt").read_text(encoding="utf-8") == "hello\n"

    # An ordinary run now works without any further prompting.
    second = _run(env, job)
    assert second.status == RunStatus.success, second.summary
    assert second.is_resync is False


# --------------------------------------------------------------------------
# Criterion: independent creates converge
# --------------------------------------------------------------------------


def test_files_created_on_both_sides_converge(tmp_path: Path, env) -> None:
    p1, p2 = _pair(tmp_path)
    (p1 / "shared.txt").write_text("shared\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2)
    assert _run(env, job, resync=True).status == RunStatus.success

    (p1 / "from-one.txt").write_text("one\n", encoding="utf-8")
    (p2 / "from-two.txt").write_text("two\n", encoding="utf-8")

    run = _run(env, job)
    assert run.status == RunStatus.success, run.summary

    expected = {"shared.txt", "from-one.txt", "from-two.txt"}
    assert {path.name for path in p1.iterdir()} == expected
    assert {path.name for path in p2.iterdir()} == expected
    # And the contents actually crossed, rather than empty files appearing.
    assert (p2 / "from-one.txt").read_text(encoding="utf-8") == "one\n"
    assert (p1 / "from-two.txt").read_text(encoding="utf-8") == "two\n"


# --------------------------------------------------------------------------
# Criterion: an edit-edit conflict loses nothing
# --------------------------------------------------------------------------


def test_edit_edit_conflict_keeps_both_versions(tmp_path: Path, env) -> None:
    """The heart of the milestone. Whatever the winner is, the losing version
    must still exist somewhere afterwards."""
    import time

    p1, p2 = _pair(tmp_path)
    (p1 / "doc.txt").write_text("original\n", encoding="utf-8")
    # Quiet companions. bisync has a second safety abort, "all files were
    # changed", so a tree whose only file is the conflicting one is refused
    # before any conflict handling happens.
    for index in range(4):
        (p1 / f"quiet{index}.txt").write_text("unchanged\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2, conflict_resolve=ConflictResolve.newer)
    assert _run(env, job, resync=True).status == RunStatus.success

    (p1 / "doc.txt").write_text("edited on one\n", encoding="utf-8")
    time.sleep(1.1)
    (p2 / "doc.txt").write_text("edited on two\n", encoding="utf-8")

    run = _run(env, job)
    assert run.status == RunStatus.success, run.summary

    # The newer edit wins on both sides.
    assert (p1 / "doc.txt").read_text(encoding="utf-8") == "edited on two\n"
    assert (p2 / "doc.txt").read_text(encoding="utf-8") == "edited on two\n"

    # And the loser survives under a conflict name. Nothing is discarded.
    conflicts = sorted(path.name for path in p1.iterdir() if "conflict" in path.name)
    assert conflicts, f"the losing version was lost: {sorted(p.name for p in p1.iterdir())}"
    loser = (p1 / conflicts[0]).read_text(encoding="utf-8")
    assert loser == "edited on one\n"


def test_conflict_resolution_can_prefer_a_side(tmp_path: Path, env) -> None:
    import time

    p1, p2 = _pair(tmp_path)
    (p1 / "doc.txt").write_text("original\n", encoding="utf-8")
    for index in range(4):
        (p1 / f"quiet{index}.txt").write_text("unchanged\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2, conflict_resolve=ConflictResolve.path1)
    assert _run(env, job, resync=True).status == RunStatus.success

    (p1 / "doc.txt").write_text("path one wins\n", encoding="utf-8")
    time.sleep(1.1)
    (p2 / "doc.txt").write_text("path two is newer\n", encoding="utf-8")

    run = _run(env, job)
    assert run.status == RunStatus.success, run.summary
    # path1 preferred even though path2 is newer.
    assert (p1 / "doc.txt").read_text(encoding="utf-8") == "path one wins\n"
    assert (p2 / "doc.txt").read_text(encoding="utf-8") == "path one wins\n"
    assert any("conflict" in path.name for path in p2.iterdir())


# --------------------------------------------------------------------------
# Criterion: a wiped workdir surfaces the prompt
# --------------------------------------------------------------------------


def test_wiped_workdir_asks_for_a_resync_rather_than_failing_silently(tmp_path: Path, env) -> None:
    """The workdir can be lost independently of the database, so rclone's own
    message is the source of truth and it clears bisync_initialized."""
    settings, _box, _factory, session = env
    p1, p2 = _pair(tmp_path)
    (p1 / "a.txt").write_text("hello\n", encoding="utf-8")
    job = _job(session, p1, p2)
    assert _run(env, job, resync=True).status == RunStatus.success
    session.refresh(job)
    assert job.bisync_initialized is True

    workdir = Path(bisync.workdir_for(str(settings.bisync_dir), job.id))
    for listing in workdir.glob("*"):
        listing.unlink()

    run = _run(env, job)
    assert run.status == RunStatus.failed
    message = run.summary["error"]
    assert "first sync" in message.lower()
    assert "Nothing was changed" in message

    # The flag is corrected, so the UI offers the prompt rather than insisting
    # the job is initialised.
    session.refresh(job)
    assert job.bisync_initialized is False

    # And a resync recovers it.
    assert _run(env, job, resync=True).status == RunStatus.success
    session.refresh(job)
    assert job.bisync_initialized is True


# --------------------------------------------------------------------------
# The delete brake, which means something different here
# --------------------------------------------------------------------------


def test_max_delete_is_a_percentage_for_bisync(tmp_path: Path, env) -> None:
    """Verified against rclone: --max-delete is a percentage for bisync and a
    count for sync. Reusing the count conversion would disable the brake."""
    _settings, _box, _factory, session = env
    p1, p2 = _pair(tmp_path)
    for index in range(10):
        (p1 / f"f{index}.txt").write_text("v\n", encoding="utf-8")

    job = _job(session, p1, p2, max_delete_pct=20)
    assert _run(env, job, resync=True).status == RunStatus.success

    # Delete 3 of 10, which is 30% and over the 20% brake.
    for index in range(3):
        (p1 / f"f{index}.txt").unlink()

    run = _run(env, job)
    assert run.status == RunStatus.failed
    assert "too many deletes" in run.summary["error"]
    # Nothing was propagated: all ten survive on the other side.
    assert len(list(p2.iterdir())) == 10


def test_deletions_within_the_brake_propagate(tmp_path: Path, env) -> None:
    _settings, _box, _factory, session = env
    p1, p2 = _pair(tmp_path)
    for index in range(10):
        (p1 / f"f{index}.txt").write_text("v\n", encoding="utf-8")

    job = _job(session, p1, p2, max_delete_pct=50)
    assert _run(env, job, resync=True).status == RunStatus.success

    (p1 / "f0.txt").unlink()
    run = _run(env, job)
    assert run.status == RunStatus.success, run.summary
    assert not (p2 / "f0.txt").exists(), "the deletion did not propagate"
    assert len(list(p2.iterdir())) == 9


def test_the_command_always_carries_the_brake_as_a_percentage() -> None:
    """Invariant 7, with the unit that bisync actually uses."""
    from app.crypto import Redactor
    from app.engines.rcloneconf import Prepared

    prepared = Prepared(endpoints={}, env={}, base_args=["--config", ""], redactor=Redactor([]))
    job = Job(
        name="x",
        direction=Direction.bidirectional,
        conflict_resolve=ConflictResolve.newer,
        max_delete_pct=20,
        modify_window="1s",
        filters={},
        check_access=False,
    )
    argv = bisync.build_bisync_command(job, prepared, "a:", "b:", workdir="/w")
    assert "--max-delete" in argv
    # The percentage, not a resolved count.
    assert argv[argv.index("--max-delete") + 1] == "20"
    assert argv[argv.index("--conflict-loser") + 1] == "num"
    assert argv[argv.index("--workdir") + 1] == "/w"


def test_a_wholesale_change_on_one_side_is_refused(tmp_path: Path, env) -> None:
    """bisync has a second safety abort beyond the delete brake: it refuses when
    every file on a side changed, which is what a restored-from-backup or a
    re-encrypted tree looks like.

    Pinned because it surprised me, and because the reason has to reach the
    operator rather than surface as a generic failure.
    """
    _settings, _box, _factory, session = env
    p1, p2 = _pair(tmp_path)
    for index in range(3):
        (p1 / f"f{index}.txt").write_text("before\n", encoding="utf-8")

    job = _job(session, p1, p2)
    assert _run(env, job, resync=True).status == RunStatus.success

    for index in range(3):
        (p1 / f"f{index}.txt").write_text("after, everything rewritten\n", encoding="utf-8")

    run = _run(env, job)
    assert run.status == RunStatus.failed
    message = run.summary["error"]
    assert "all files were changed" in message
    # The other side is untouched.
    assert (p2 / "f0.txt").read_text(encoding="utf-8") == "before\n"


def test_modify_window_is_dropped_for_time_based_conflict_policies() -> None:
    """A nonzero --modify-window disables bisync's newer/older policies entirely.

    Verified against rclone with versions ten seconds apart and a one second
    window: no winner is chosen, both versions are renamed to .conflict1 and
    .conflict2, and the file vanishes from its original name. Passing the flag
    would silently discard the policy the operator chose.
    """
    from app.crypto import Redactor
    from app.engines.rcloneconf import Prepared

    prepared = Prepared(endpoints={}, env={}, base_args=["--config", ""], redactor=Redactor([]))

    def argv_for(policy: ConflictResolve) -> list[str]:
        job = Job(
            name="x",
            direction=Direction.bidirectional,
            conflict_resolve=policy,
            max_delete_pct=20,
            modify_window="1s",
            filters={},
            check_access=False,
        )
        return bisync.build_bisync_command(job, prepared, "a:", "b:", workdir="/w")

    assert "--modify-window" not in argv_for(ConflictResolve.newer)
    assert "--modify-window" not in argv_for(ConflictResolve.older)
    # A policy that does not compare times keeps the drift protection.
    assert "--modify-window" in argv_for(ConflictResolve.path1)
    assert "--modify-window" in argv_for(ConflictResolve.larger)


def test_check_access_is_opt_in() -> None:
    from app.crypto import Redactor
    from app.engines.rcloneconf import Prepared

    prepared = Prepared(endpoints={}, env={}, base_args=["--config", ""], redactor=Redactor([]))

    def argv_with(check: bool) -> list[str]:
        job = Job(
            name="x",
            direction=Direction.bidirectional,
            conflict_resolve=ConflictResolve.newer,
            max_delete_pct=20,
            modify_window="1s",
            filters={},
            check_access=check,
        )
        return bisync.build_bisync_command(job, prepared, "a:", "b:", workdir="/w")

    assert "--check-access" not in argv_with(False)
    assert "--check-access" in argv_with(True)


# --------------------------------------------------------------------------
# Dry running a bidirectional job
#
# This was refused outright until BisyncEngine existed, so the one direction
# that can damage both copies was the one that could not be previewed.
# --------------------------------------------------------------------------


def _dry_run(env, job: Job) -> JobRun:
    settings, box, factory, session = env
    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)
    planner.PlanRunner(factory, box=box, settings=settings).run_now(run.id)
    session.expire_all()
    stored = session.get(JobRun, run.id)
    assert stored is not None
    return stored


def test_a_bidirectional_dry_run_reports_both_sides(tmp_path: Path, env) -> None:
    """The whole point: see what would happen to each side before it happens."""
    p1, p2 = _pair(tmp_path)
    (p1 / "shared.txt").write_text("same\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2)

    assert _run(env, job, resync=True).status == RunStatus.success

    # One new file on each side, and one removed from path1.
    (p1 / "only-on-one.txt").write_text("one\n", encoding="utf-8")
    (p2 / "only-on-two.txt").write_text("two\n", encoding="utf-8")
    (p1 / "shared.txt").unlink()

    run = _dry_run(env, job)

    assert run.status == RunStatus.success, run.summary
    changes = session.query(JobRunChange).filter_by(run_id=run.id).all()
    by_path = {c.path: (c.side.value, c.action.value) for c in changes}
    # The side is where the change lands, matching what `rclone.side_for`
    # documents: a file that appeared on path1 is copied to path2, and a file
    # removed from path1 is removed from path2.
    assert by_path["only-on-one.txt"] == ("dest", "new")
    assert by_path["only-on-two.txt"] == ("source", "new")
    assert by_path["shared.txt"] == ("dest", "deleted")


def test_a_dry_run_changes_nothing_on_either_side(tmp_path: Path, env) -> None:
    """A dry run modifies nothing. That is the invariant, and bisync writes to
    both sides, so there are two chances to break it."""
    p1, p2 = _pair(tmp_path)
    (p1 / "shared.txt").write_text("same\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2)
    assert _run(env, job, resync=True).status == RunStatus.success

    (p1 / "new-one.txt").write_text("one\n", encoding="utf-8")
    (p2 / "new-two.txt").write_text("two\n", encoding="utf-8")
    before1 = sorted(f.name for f in p1.iterdir())
    before2 = sorted(f.name for f in p2.iterdir())

    _dry_run(env, job)

    assert sorted(f.name for f in p1.iterdir()) == before1
    assert sorted(f.name for f in p2.iterdir()) == before2


def test_a_job_with_no_first_sync_says_so_rather_than_reporting_nothing(
    tmp_path: Path, env
) -> None:
    """An empty plan would read as "nothing would change", which is the opposite
    of the truth: nothing is known yet."""
    p1, p2 = _pair(tmp_path)
    (p1 / "a.txt").write_text("hello\n", encoding="utf-8")
    _settings, _box, _factory, session = env

    run = _dry_run(env, _job(session, p1, p2))

    assert run.status == RunStatus.failed
    assert "first sync" in run.summary["error"].lower()


def test_the_brake_is_reported_as_a_percentage_not_a_count(tmp_path: Path, env) -> None:
    """--max-delete is a percentage for bisync and a count for sync. Reporting a
    resolved count would name a threshold rclone does not enforce, on the one
    direction that can damage both copies."""
    p1, p2 = _pair(tmp_path)
    (p1 / "a.txt").write_text("hello\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2, max_delete_pct=25)
    assert _run(env, job, resync=True).status == RunStatus.success

    run = _dry_run(env, job)

    assert run.summary["bidirectional"] is True
    assert run.summary["max_delete_threshold"] == 25


def test_a_live_run_records_the_files_it_moved(tmp_path: Path, env) -> None:
    """The counts were right and the change table said "Nothing changed",
    because only the one way runner ever wrote per-file rows."""
    p1, p2 = _pair(tmp_path)
    (p1 / "shared.txt").write_text("same\n", encoding="utf-8")
    _settings, _box, _factory, session = env
    job = _job(session, p1, p2)
    assert _run(env, job, resync=True).status == RunStatus.success

    (p1 / "from-one.txt").write_text("one\n", encoding="utf-8")
    (p2 / "from-two.txt").write_text("two\n", encoding="utf-8")

    run = _run(env, job)

    assert run.status == RunStatus.success, run.summary
    changes = session.query(JobRunChange).filter_by(run_id=run.id).all()
    by_path = {c.path: c.side.value for c in changes}
    assert "from-one.txt" in by_path, f"only recorded {sorted(by_path)}"
    assert "from-two.txt" in by_path
    # Landing sides, so a file that appeared on path1 lands on the destination.
    assert by_path["from-one.txt"] == "dest"
    assert by_path["from-two.txt"] == "source"


def test_a_first_sync_counts_what_it_copied(tmp_path: Path, env) -> None:
    """A resync emits no per-side delta lines, so counts derived from those read
    zero for a first sync that had plainly copied a file."""
    p1, p2 = _pair(tmp_path)
    (p1 / "a.txt").write_text("hello\n", encoding="utf-8")
    (p1 / "b.txt").write_text("world\n", encoding="utf-8")
    _settings, _box, _factory, session = env

    run = _run(env, _job(session, p1, p2), resync=True)

    assert run.status == RunStatus.success, run.summary
    assert run.summary["resync"] is True
    assert run.summary["new"] == 2, run.summary
    assert run.files_transferred == 2
    # And the files are listed, not just counted.
    changes = session.query(JobRunChange).filter_by(run_id=run.id).all()
    assert sorted(c.path for c in changes) == ["a.txt", "b.txt"]
