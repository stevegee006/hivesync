"""Schema behaviour tests.

These assert on constraints doing their job, not on the models merely importing.
Two of them cover invariants the spec states but leaves to the implementation:
the one-active-run-per-job rule from SPEC section 6.2, and RESTRICT protecting a
credential or connection that something depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.models import (
    Connection,
    ConnectionType,
    Credential,
    CredentialKind,
    FilterPreset,
    Job,
    JobRun,
    RunMode,
    RunStatus,
    RunTrigger,
)
from tests.conftest import create_schema, make_settings


@pytest.fixture
def db(tmp_path: Path) -> Session:
    settings: Settings = make_settings(tmp_path)
    create_schema(settings)
    engine = create_db_engine(settings)
    return create_session_factory(engine)()


def _connection(name: str) -> Connection:
    return Connection(name=name, type=ConnectionType.sftp, host="example.test", base_path="/srv")


def _job(source: Connection, dest: Connection) -> Job:
    return Job(
        name="nightly",
        source_connection_id=source.id,
        source_path="www",
        dest_connection_id=dest.id,
        dest_path="Media/www",
    )


def _run(job: Job, status: RunStatus) -> JobRun:
    return JobRun(job_id=job.id, trigger=RunTrigger.manual, mode=RunMode.live, status=status)


def test_foreign_keys_are_enforced(db: Session) -> None:
    """SQLite ignores foreign keys unless the pragma is on, which would silently
    disable every RESTRICT in the schema."""
    db.add(
        Job(
            name="orphan",
            source_connection_id=999,
            dest_connection_id=998,
            source_path="",
            dest_path="",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_defaults_applied(db: Session) -> None:
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    job = _job(source, dest)
    db.add(job)
    db.commit()

    assert job.enabled is True
    assert job.max_delete_pct == 20
    assert job.modify_window == "1s"
    assert job.bisync_initialized is False
    assert job.created_at.tzinfo is not None


def test_json_columns_roundtrip(db: Session) -> None:
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    job = _job(source, dest)
    job.filters = {"exclude": ["**/@eaDir/**"], "min_size": "1k"}
    db.add(job)
    db.commit()
    db.expire_all()

    reloaded = db.get(Job, job.id)
    assert reloaded is not None
    assert reloaded.filters["exclude"] == ["**/@eaDir/**"]


def test_credential_in_use_cannot_be_deleted(db: Session) -> None:
    credential = Credential(
        name="sftp-password", kind=CredentialKind.password, secret_ciphertext=b"ciphertext"
    )
    db.add(credential)
    db.commit()

    connection = _connection("uses-credential")
    connection.credential_id = credential.id
    db.add(connection)
    db.commit()

    db.delete(credential)
    with pytest.raises(IntegrityError):
        db.commit()


def test_connection_in_use_cannot_be_deleted(db: Session) -> None:
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    db.add(_job(source, dest))
    db.commit()

    db.delete(source)
    with pytest.raises(IntegrityError):
        db.commit()


def test_only_one_active_run_per_job(db: Session) -> None:
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    job = _job(source, dest)
    db.add(job)
    db.commit()

    db.add(_run(job, RunStatus.running))
    db.commit()

    db.add(_run(job, RunStatus.queued))
    with pytest.raises(IntegrityError):
        db.commit()


def test_finished_runs_do_not_block_a_new_one(db: Session) -> None:
    """The index is partial, so history must not collide with a fresh run."""
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    job = _job(source, dest)
    db.add(job)
    db.commit()

    for status in (RunStatus.success, RunStatus.failed, RunStatus.cancelled, RunStatus.skipped):
        db.add(_run(job, status))
    db.commit()

    db.add(_run(job, RunStatus.running))
    db.commit()

    assert len(job.runs) == 5


def test_two_jobs_can_run_at_once(db: Session) -> None:
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    first = _job(source, dest)
    second = Job(
        name="second",
        source_connection_id=source.id,
        source_path="other",
        dest_connection_id=dest.id,
        dest_path="Media/other",
    )
    db.add_all([first, second])
    db.commit()

    db.add_all([_run(first, RunStatus.running), _run(second, RunStatus.running)])
    db.commit()


def test_deleting_a_job_removes_its_runs(db: Session) -> None:
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    job = _job(source, dest)
    db.add(job)
    db.commit()
    db.add(_run(job, RunStatus.success))
    db.commit()

    db.delete(job)
    db.commit()

    assert db.query(JobRun).count() == 0


def test_filter_preset_association(db: Session) -> None:
    """Preset membership is a real relationship, not ids inside a JSON blob, so a
    preset that a job depends on cannot be deleted."""
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    preset = FilterPreset(name="Synology / DSM", builtin=True, rules={"exclude": ["**/@eaDir/**"]})
    db.add(preset)
    db.commit()

    job = _job(source, dest)
    job.filter_presets.append(preset)
    db.add(job)
    db.commit()
    db.expire_all()

    reloaded = db.get(Job, job.id)
    assert reloaded is not None
    assert [p.name for p in reloaded.filter_presets] == ["Synology / DSM"]

    db.delete(preset)
    with pytest.raises(IntegrityError):
        db.commit()


def test_enum_columns_store_values(db: Session) -> None:
    """The partial index predicate compares against the stored literal, so what
    lands in the column matters."""
    source, dest = _connection("src"), _connection("dst")
    db.add_all([source, dest])
    db.commit()
    job = _job(source, dest)
    db.add(job)
    db.commit()
    db.add(_run(job, RunStatus.running))
    db.commit()

    stored = db.execute(
        JobRun.__table__.select().with_only_columns(JobRun.__table__.c.status)
    ).scalar_one()
    assert stored == "running"
