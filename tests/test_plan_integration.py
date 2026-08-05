"""M2's acceptance criterion, against real rclone.

    a dry run against a fixture holding one new, one changed, one deleted and one
    identical file produces exactly those four classifications and modifies
    nothing on either side.

Runs inside the container so `rclone` is the pinned binary. Two local directories
are used rather than the network fixtures: the classification logic is what is
under test, and a local pair makes "modified nothing" checkable byte for byte.

    make test-integration
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.crypto import SecretBox
from app.db import create_db_engine
from app.engines.base import EngineError
from app.engines.rclone import RcloneEngine
from app.jobs import planner
from app.models import (
    ChangeAction,
    CompareMode,
    Connection,
    ConnectionType,
    Direction,
    Job,
    RunMode,
    RunStatus,
    RunTrigger,
)
from tests.conftest import create_schema, make_settings

pytestmark = pytest.mark.integration


def _fingerprint(*roots: Path) -> str:
    """A hash of every file's path and contents, to prove nothing changed."""
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """The exact fixture the criterion names."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    dst.mkdir()

    (src / "identical.txt").write_text("same contents\n", encoding="utf-8")
    (dst / "identical.txt").write_text("same contents\n", encoding="utf-8")

    (src / "changed.txt").write_text("the new contents, longer\n", encoding="utf-8")
    (dst / "changed.txt").write_text("old\n", encoding="utf-8")

    (src / "new.txt").write_text("brand new\n", encoding="utf-8")
    (dst / "deleted.txt").write_text("about to go\n", encoding="utf-8")
    return src, dst


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path / "config")
    create_schema(settings)
    session = sessionmaker(bind=create_db_engine(settings))()
    return settings, SecretBox(settings.secret_key), session


def _job_over(session, src: Path, dst: Path, **overrides) -> Job:  # noqa: ANN003
    source = Connection(name="src-local", type=ConnectionType.local, base_path=str(src))
    dest = Connection(name="dst-local", type=ConnectionType.local, base_path=str(dst))
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "fixture-job",
        "source_connection_id": source.id,
        "source_path": "",
        "dest_connection_id": dest.id,
        "dest_path": "",
        "direction": Direction.source_to_dest,
        "compare_mode": CompareMode.mtime_size,
        "max_delete_pct": 20,
        "filters": {},
    }
    fields.update(overrides)
    job = Job(**fields)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_four_classifications_and_nothing_modified(tree, env) -> None:
    """The M2 acceptance criterion, in full."""
    src, dst = tree
    settings, box, session = env
    before = _fingerprint(src, dst)

    job = _job_over(session, src, dst)
    plan = RcloneEngine().plan(job, box=box, settings=settings)

    by_action = {
        action: sorted(c.path for c in plan.changes if c.action == action)
        for action in (ChangeAction.new, ChangeAction.updated, ChangeAction.deleted)
    }
    assert by_action[ChangeAction.new] == ["new.txt"]
    assert by_action[ChangeAction.updated] == ["changed.txt"]
    assert by_action[ChangeAction.deleted] == ["deleted.txt"]
    assert plan.unchanged_count == 1

    # Exactly those, and nothing else.
    assert len(plan.changes) == 3
    assert plan.error_count == 0

    assert _fingerprint(src, dst) == before, "a dry run must modify nothing"


def test_new_and_updated_are_distinguished(tree, env) -> None:
    """rclone reports both as skipped=copy, so presence from phase 1 is what
    separates them. Getting this wrong would call every new file an update."""
    src, dst = tree
    settings, box, session = env
    plan = RcloneEngine().plan(_job_over(session, src, dst), box=box, settings=settings)
    assert plan.new_count == 1
    assert plan.updated_count == 1


def test_delete_direction_is_not_inverted(tree, env) -> None:
    """SPEC section 8's --combined legend is inverted. If the engine followed it,
    new.txt and deleted.txt would swap, and the review table would say 'delete'
    about a file it was going to create."""
    src, dst = tree
    settings, box, session = env
    plan = RcloneEngine().plan(_job_over(session, src, dst), box=box, settings=settings)
    deleted = [c.path for c in plan.changes if c.action == ChangeAction.deleted]
    created = [c.path for c in plan.changes if c.action == ChangeAction.new]
    assert deleted == ["deleted.txt"], "the deleted file is the one only on the destination"
    assert created == ["new.txt"], "the new file is the one only on the source"


def test_dest_file_count_feeds_the_delete_brake(tree, env) -> None:
    src, dst = tree
    settings, box, session = env
    plan = RcloneEngine().plan(_job_over(session, src, dst), box=box, settings=settings)
    # identical.txt, changed.txt, deleted.txt exist on the destination.
    assert plan.dest_file_count == 3


def test_small_tree_still_allows_one_deletion(tree, env) -> None:
    """Deliberate: the resolved threshold has a floor of one.

    Three destination files at a 20% brake is a limit of 0.6, and a brake that
    rounded that down would block every deletion on a small tree forever. One
    deletion out of three is therefore permitted and does not warn.
    """
    src, dst = tree
    settings, box, session = env
    plan = RcloneEngine().plan(
        _job_over(session, src, dst, max_delete_pct=20), box=box, settings=settings
    )
    assert plan.deleted_count == 1
    assert not any("delete brake" in warning for warning in plan.warnings)


def test_brake_warning_when_deletions_exceed_the_limit(tmp_path: Path, env) -> None:
    """A destination large enough for the percentage to bite: 3 deletions out of
    10 files against a 20% brake, a limit of 2."""
    settings, box, session = env
    src = tmp_path / "b-src"
    dst = tmp_path / "b-dst"
    src.mkdir()
    dst.mkdir()
    for index in range(7):
        (src / f"keep{index}.txt").write_text("keep\n", encoding="utf-8")
        (dst / f"keep{index}.txt").write_text("keep\n", encoding="utf-8")
    for index in range(3):
        (dst / f"gone{index}.txt").write_text("gone\n", encoding="utf-8")

    plan = RcloneEngine().plan(
        _job_over(session, src, dst, max_delete_pct=20), box=box, settings=settings
    )
    assert plan.dest_file_count == 10
    assert plan.deleted_count == 3
    warning = next(w for w in plan.warnings if "delete brake" in w)
    assert "a limit of 2" in warning
    assert "10 files" in warning


def test_no_brake_warning_when_within_the_limit(tree, env) -> None:
    src, dst = tree
    settings, box, session = env
    plan = RcloneEngine().plan(
        _job_over(session, src, dst, max_delete_pct=100), box=box, settings=settings
    )
    assert not any("delete brake" in warning for warning in plan.warnings)


def test_filters_change_the_plan(tree, env) -> None:
    """A plan that ignored filters would not be truthful."""
    src, dst = tree
    settings, box, session = env
    job = _job_over(session, src, dst, filters={"exclude": ["new.txt"]})
    plan = RcloneEngine().plan(job, box=box, settings=settings)
    assert "new.txt" not in [c.path for c in plan.changes]
    assert plan.new_count == 0


def test_direction_reversal_inverts_the_plan(tree, env) -> None:
    """Reading from the destination makes the previously-new file the deletion."""
    src, dst = tree
    settings, box, session = env
    job = _job_over(session, src, dst, direction=Direction.dest_to_source)
    plan = RcloneEngine().plan(job, box=box, settings=settings)
    assert [c.path for c in plan.changes if c.action == ChangeAction.new] == ["deleted.txt"]
    assert [c.path for c in plan.changes if c.action == ChangeAction.deleted] == ["new.txt"]


def test_size_only_misses_a_same_size_change(tree, env) -> None:
    """Verified against rclone: a content change that does not change size is
    invisible to size-only comparison. The plan must warn rather than imply the
    tree is clean."""
    src, dst = tree
    settings, box, session = env
    (src / "samesize.txt").write_text("AAAA\n", encoding="utf-8")
    (dst / "samesize.txt").write_text("BBBB\n", encoding="utf-8")

    job = _job_over(session, src, dst, compare_mode=CompareMode.size_only)
    plan = RcloneEngine().plan(job, box=box, settings=settings)
    assert "samesize.txt" not in [c.path for c in plan.changes]
    assert any("size only" in warning for warning in plan.warnings)


def test_checksum_catches_what_size_only_misses(tree, env) -> None:
    src, dst = tree
    settings, box, session = env
    (src / "samesize.txt").write_text("AAAA\n", encoding="utf-8")
    (dst / "samesize.txt").write_text("BBBB\n", encoding="utf-8")

    job = _job_over(session, src, dst, compare_mode=CompareMode.checksum)
    job.source_connection.capabilities = {"Precision": 1, "Hashes": ["md5"], "Features": {}}
    job.dest_connection.capabilities = {"Precision": 1, "Hashes": ["md5"], "Features": {}}
    session.commit()

    plan = RcloneEngine().plan(job, box=box, settings=settings)
    assert "samesize.txt" in [c.path for c in plan.changes]


def test_empty_source_produces_a_plan_that_deletes_everything(tmp_path: Path, env) -> None:
    """The mass-delete scenario the brake exists for. The plan must show it
    rather than the run discovering it."""
    settings, box, session = env
    src = tmp_path / "empty-src"
    dst = tmp_path / "full-dst"
    src.mkdir()
    dst.mkdir()
    for index in range(10):
        (dst / f"file{index}.txt").write_text("data\n", encoding="utf-8")

    plan = RcloneEngine().plan(_job_over(session, src, dst), box=box, settings=settings)
    assert plan.deleted_count == 10
    assert plan.new_count == 0
    assert any("delete brake" in warning for warning in plan.warnings)
    assert (dst / "file0.txt").exists(), "a dry run must not delete anything"


# --------------------------------------------------------------------------
# The run lifecycle around the plan
# --------------------------------------------------------------------------


def test_run_records_the_plan(tree, env) -> None:
    src, dst = tree
    settings, box, session = env
    job = _job_over(session, src, dst)

    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)
    runner = planner.PlanRunner(
        sessionmaker(bind=create_db_engine(settings)), box=box, settings=settings
    )
    runner.run_now(run.id)

    session.expire_all()
    stored = session.get(type(run), run.id)
    assert stored is not None
    assert stored.status == RunStatus.success
    assert stored.files_transferred == 2
    assert stored.files_deleted == 1
    assert stored.summary is not None
    assert stored.summary["unchanged"] == 1
    assert stored.summary["dest_file_count"] == 3
    # The stored command is redacted and safe to display. SPEC 6.1.
    assert stored.command_redacted
    assert "rclone" in stored.command_redacted
    assert len(stored.changes) == 3


def test_second_run_while_one_is_active_is_refused(tree, env) -> None:
    """SPEC 6.2, enforced by the database rather than by a prior SELECT."""
    src, dst = tree
    _settings, _box, session = env
    job = _job_over(session, src, dst)
    planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)
    with pytest.raises(planner.RunConflict):
        planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)


def test_a_broken_job_fails_the_run_rather_than_the_process(env, tmp_path: Path) -> None:
    """An unreachable endpoint is a result to read, not a crashed worker."""
    settings, box, session = env
    source = Connection(
        name="nowhere", type=ConnectionType.sftp, host="203.0.113.1", port=22, base_path="/x"
    )
    dest = Connection(name="local-dst", type=ConnectionType.local, base_path=str(tmp_path / "dest"))
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="broken",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        source_path="",
        dest_path="",
        filters={},
    )
    session.add(job)
    session.commit()

    run = planner.create_run(session, job, trigger=RunTrigger.manual, mode=RunMode.dry_run)
    runner = planner.PlanRunner(
        sessionmaker(bind=create_db_engine(settings)), box=box, settings=settings
    )
    runner.run_now(run.id)

    session.expire_all()
    stored = session.get(type(run), run.id)
    assert stored is not None
    assert stored.status == RunStatus.failed
    assert stored.summary is not None
    assert stored.summary["error"]


def test_live_mode_is_refused(tree, env) -> None:
    src, dst = tree
    settings, box, session = env
    job = _job_over(session, src, dst)
    with pytest.raises(EngineError, match="not implemented"):
        RcloneEngine().execute(job, box=box, settings=settings)


def test_untested_endpoints_warn_about_being_untested(tree, env) -> None:
    """Not the same as having no hashes. Both endpoints here are local, which
    exposes every hash type; they simply have not been probed."""
    src, dst = tree
    settings, box, session = env
    plan = RcloneEngine().plan(_job_over(session, src, dst), box=box, settings=settings)
    warning = next(w for w in plan.warnings if "not been tested" in w)
    assert "src-local" in warning
    assert "share no hash type" not in " ".join(plan.warnings)


def test_probed_endpoints_with_hashes_produce_no_hash_warning(tree, env) -> None:
    src, dst = tree
    settings, box, session = env
    job = _job_over(session, src, dst)
    probe_payload = {"Precision": 1, "Hashes": ["md5"], "Features": {"Move": True}}
    job.source_connection.capabilities = probe_payload
    job.dest_connection.capabilities = probe_payload
    session.commit()

    plan = RcloneEngine().plan(job, box=box, settings=settings)
    assert not any("hash" in w for w in plan.warnings)
