"""M6's acceptance criteria, against real rclone.

    a deleted source file lands in the archive with its relative path preserved,
    the archive tree is never itself synced or re-archived across three
    explanation, and the manual checklist passes.

The last criterion is written around Synology in the spec. It is run here against
the SMB fixture instead, because what matters is SMB rather than one vendor's
implementation of it. The multi-gigabyte case from the spec's checklist is not
covered: see test_smb_archive_checklist for what is and is not.

    make test-integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.crypto import SecretBox
from app.db import create_db_engine
from app.jobs import planner
from app.jobs.runner import LiveRunner
from app.models import (
    ArchiveLayout,
    Connection,
    ConnectionType,
    Credential,
    CredentialKind,
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

SMB_HOST = os.environ.get("HIVESYNC_TEST_SMB_HOST", "smb")
SMB_PORT = int(os.environ.get("HIVESYNC_TEST_SMB_PORT", "445"))


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path / "config")
    create_schema(settings)
    factory = sessionmaker(bind=create_db_engine(settings))
    return settings, SecretBox(settings.secret_key), factory, factory()


def _local_job(session, src: Path, dst: Path, **overrides) -> Job:  # noqa: ANN003
    source = Connection(name="arc-src", type=ConnectionType.local, base_path=str(src))
    dest = Connection(name="arc-dst", type=ConnectionType.local, base_path=str(dst))
    session.add_all([source, dest])
    session.commit()
    fields: dict = {
        "name": "Archiving Job",
        "source_connection_id": source.id,
        "dest_connection_id": dest.id,
        "source_path": "",
        "dest_path": "",
        "direction": Direction.source_to_dest,
        "delete_mode": DeleteMode.archive,
        "archive_layout": ArchiveLayout.timestamped_dir,
        "max_delete_pct": 100,
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


def _files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------
# Criterion: a deleted file lands in the archive with its path preserved
# --------------------------------------------------------------------------


def test_deleted_file_is_archived_with_its_relative_path(tmp_path: Path, env) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "keep").mkdir(parents=True)
    (dst / "keep").mkdir(parents=True)
    (dst / "sub" / "deep").mkdir(parents=True)
    (src / "keep" / "a.txt").write_text("stays\n", encoding="utf-8")
    (dst / "keep" / "a.txt").write_text("stays\n", encoding="utf-8")
    (dst / "sub" / "deep" / "gone.txt").write_text("archive me\n", encoding="utf-8")

    _settings, _box, _factory, session = env
    run = _run(env, _local_job(session, src, dst))
    assert run.status == RunStatus.success, run.summary

    # Gone from the destination.
    assert not (dst / "sub" / "deep" / "gone.txt").exists()

    # Present in the sibling archive, with the nesting intact.
    archive_root = tmp_path / "dst.hivesync-archive"
    archived = _files_under(archive_root)
    assert len(archived) == 1
    only = next(iter(archived))
    assert only.endswith(str(Path("sub") / "deep" / "gone.txt")), only
    # And under the job and run stamp, per SPEC 7.2.
    assert "archiving-job" in only
    assert (archive_root / only).read_text(encoding="utf-8") == "archive me\n"


def test_the_suffix_layout_keeps_the_extension(tmp_path: Path, env) -> None:
    src = tmp_path / "s"
    dst = tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "keep.txt").write_text("k\n", encoding="utf-8")
    (dst / "keep.txt").write_text("k\n", encoding="utf-8")
    (dst / "report.txt").write_text("old report\n", encoding="utf-8")

    _settings, _box, _factory, session = env
    job = _local_job(session, src, dst, archive_layout=ArchiveLayout.suffix)
    assert _run(env, job).status == RunStatus.success

    archived = _files_under(tmp_path / "d.hivesync-archive")
    assert len(archived) == 1
    name = next(iter(archived))
    assert name.startswith("report.")
    assert name.endswith(".txt"), f"the extension was not preserved: {name}"


# --------------------------------------------------------------------------
# Criterion: the archive is never itself synced or re-archived
# --------------------------------------------------------------------------


def test_a_child_archive_is_stable_across_three_runs(tmp_path: Path, env) -> None:
    """SPEC 7.1 says an overlapping archive gets re-archived forever. Verified
    against rclone 1.74.4, it instead refuses the run outright unless the exclude
    is injected. Either way the injected exclude is what makes this work, and
    three runs is what proves it stays put."""
    src = tmp_path / "s"
    dst = tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "keep.txt").write_text("k\n", encoding="utf-8")
    (dst / "keep.txt").write_text("k\n", encoding="utf-8")
    (dst / "doomed.txt").write_text("bye\n", encoding="utf-8")

    _settings, _box, _factory, session = env
    # An archive deliberately inside the destination.
    job = _local_job(session, src, dst, archive_base=str(dst / ".attic"))

    snapshots = []
    for _ in range(3):
        run = _run(env, job)
        assert run.status == RunStatus.success, run.summary
        snapshots.append(_files_under(dst))
        # A finished run frees the job for the next one.
        session.expire_all()

    # The destination settles: keep.txt plus the one archived file, unchanged
    # from the second run onward.
    assert snapshots[1] == snapshots[2], f"the archive moved between runs: {snapshots}"
    archived = {name for name in snapshots[2] if ".attic" in name}
    assert len(archived) == 1, f"expected exactly one archived file, saw {archived}"
    assert "keep.txt" in snapshots[2]


def test_a_sibling_archive_is_stable_across_three_runs(tmp_path: Path, env) -> None:
    src = tmp_path / "s"
    dst = tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "keep.txt").write_text("k\n", encoding="utf-8")
    (dst / "keep.txt").write_text("k\n", encoding="utf-8")
    (dst / "doomed.txt").write_text("bye\n", encoding="utf-8")

    _settings, _box, _factory, session = env
    job = _local_job(session, src, dst)

    for _ in range(3):
        assert _run(env, job).status == RunStatus.success
        session.expire_all()

    # The destination holds only the kept file; the archive is outside it.
    assert _files_under(dst) == {"keep.txt"}
    assert len(_files_under(tmp_path / "d.hivesync-archive")) == 1


# --------------------------------------------------------------------------
# The checklist, against real SMB rather than a specific NAS vendor
# --------------------------------------------------------------------------


def test_smb_archive_checklist(tmp_path: Path, env) -> None:
    """SPEC 18's manual checklist, automated against the SMB fixture.

    Covers: create, update, delete into the archive, rename, and a unicode
    filename. Deliberately **not** covered: a file larger than 5 GB. That case
    is real, but it needs tens of gigabytes of disk and minutes of transfer, so
    it stays a manual check against production hardware. Nothing here should be
    read as evidence about multi-gigabyte files.
    """
    _settings, box, _factory, session = env
    credential = Credential(
        name="smb", kind=CredentialKind.password, secret_ciphertext=box.encrypt("testpass")
    )
    session.add(credential)
    session.commit()

    src = tmp_path / "smb-src"
    src.mkdir()
    source = Connection(name="smb-local", type=ConnectionType.local, base_path=str(src))
    dest = Connection(
        name="smb-share",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username="testuser",
        share="testshare",
        base_path="",
        credential_id=credential.id,
    )
    session.add_all([source, dest])
    session.commit()

    job = Job(
        name="SMB Archive",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        source_path="",
        dest_path="",
        direction=Direction.source_to_dest,
        delete_mode=DeleteMode.archive,
        max_delete_pct=100,
        filters={},
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    # 1. Create, including a unicode name.
    (src / "plain.txt").write_text("one\n", encoding="utf-8")
    (src / "ünïcode-文件.txt").write_text("two\n", encoding="utf-8")
    assert _run(env, job).status == RunStatus.success, "create failed"
    session.expire_all()

    # 2. Update.
    (src / "plain.txt").write_text("one, edited\n", encoding="utf-8")
    assert _run(env, job).status == RunStatus.success, "update failed"
    session.expire_all()

    # 3. Rename, which is a create plus a delete, so the old name is archived.
    (src / "plain.txt").rename(src / "renamed.txt")
    run = _run(env, job)
    assert run.status == RunStatus.success, "rename failed"
    assert run.files_deleted >= 1, "the old name was not removed from the destination"
    session.expire_all()

    # 4. Delete into the archive.
    (src / "ünïcode-文件.txt").unlink()
    run = _run(env, job)
    assert run.status == RunStatus.success, "delete failed"
    assert run.files_deleted >= 1

    # The archive lives inside the share, because a share root has no sibling,
    # and the destination is stable rather than re-archiving itself.
    before = run.summary
    session.expire_all()
    again = _run(env, job)
    assert again.status == RunStatus.success
    assert again.files_deleted == 0, (
        "a second run archived something again, so the archive is being synced: "
        f"{before} then {again.summary}"
    )
