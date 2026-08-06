"""Startup guards.

The fingerprint check exists because SPEC section 15 says the encryption key is
never persisted. Without a fingerprint, a swapped or lost key stays invisible
until a scheduled job fails mid run with an opaque decrypt error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import crypto
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.main import StartupError, create_app
from app.models import SECRET_KEY_FINGERPRINT, Setting
from tests.conftest import create_schema, make_settings


def test_fingerprint_recorded_on_first_boot(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path)
    create_schema(settings)
    create_app(settings)

    session = create_session_factory(create_db_engine(settings))()
    row = session.get(Setting, SECRET_KEY_FINGERPRINT)
    assert row is not None
    assert row.value == crypto.key_fingerprint(settings.secret_key)
    # The fingerprint must not be the key.
    assert row.value != settings.secret_key
    assert settings.secret_key not in (row.value or "")


def test_restart_with_the_same_key_is_fine(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path)
    create_schema(settings)
    create_app(settings)
    create_app(settings)


def test_changed_key_refuses_to_start(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path)
    create_schema(settings)
    create_app(settings)

    replacement: Settings = make_settings(
        tmp_path, secret_key=Fernet.generate_key().decode("ascii")
    )
    with pytest.raises(StartupError) as excinfo:
        create_app(replacement)
    message = str(excinfo.value)
    assert "HIVESYNC_SECRET_KEY" in message
    assert replacement.secret_key not in message
    assert settings.secret_key not in message


def test_missing_key_refuses_to_start(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path, secret_key="")
    create_schema(settings)
    with pytest.raises(crypto.CryptoKeyError):
        create_app(settings)


def test_malformed_key_refuses_to_start(tmp_path: Path) -> None:
    settings: Settings = make_settings(tmp_path, secret_key="obviously-not-a-fernet-key")
    create_schema(settings)
    with pytest.raises(crypto.CryptoKeyError):
        create_app(settings)


def test_directories_are_created(tmp_path: Path) -> None:
    target = tmp_path / "config"
    settings: Settings = make_settings(target)
    create_schema(settings)
    create_app(settings)

    assert target.is_dir()
    assert (target / "logs").is_dir()
    assert (target / "bisync").is_dir()


def test_sqlite_pragmas_are_applied(tmp_path: Path) -> None:
    """WAL and foreign_keys are both off by default and both load bearing."""
    settings: Settings = make_settings(tmp_path)
    create_schema(settings)
    engine = create_db_engine(settings)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_runs_left_by_a_restart_are_released(tmp_path: Path) -> None:
    """A row saying "running" describes a process that no longer exists.

    Nothing else will ever finish it, and the partial unique index then refuses
    every later run for that job: a scheduled job records skip after skip, and a
    continuous job stops looking with nothing in the log to say why. Found by
    restarting the container during a sync.
    """
    from sqlalchemy.orm import sessionmaker

    from app.db import create_db_engine
    from app.models import (
        Connection,
        ConnectionType,
        Job,
        JobRun,
        RunMode,
        RunStatus,
        RunTrigger,
    )

    settings = make_settings(tmp_path)
    create_schema(settings)
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="s", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="d", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    # One job per active run: the partial unique index refuses two, which is the
    # invariant that makes an orphaned row so damaging in the first place.
    for index, status in enumerate((RunStatus.running, RunStatus.queued)):
        job = Job(
            name=f"Interrupted {index}",
            source_connection_id=source.id,
            dest_connection_id=dest.id,
            filters={},
        )
        session.add(job)
        session.commit()
        session.add(
            JobRun(job_id=job.id, trigger=RunTrigger.schedule, mode=RunMode.live, status=status)
        )
        session.commit()

    create_app(settings)

    session.expire_all()
    for run in session.query(JobRun).all():
        assert run.status == RunStatus.failed
        assert run.finished_at is not None
        # Recorded rather than deleted: work may have happened on disk before
        # the process died, and the next run's brake reads the resulting state.
        assert "interrupted by a restart" in run.summary["error"]


def test_a_finished_run_is_left_alone_by_startup(tmp_path: Path) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.db import create_db_engine
    from app.models import (
        Connection,
        ConnectionType,
        Job,
        JobRun,
        RunMode,
        RunStatus,
        RunTrigger,
    )

    settings = make_settings(tmp_path)
    create_schema(settings)
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
            files_transferred=4,
        )
    )
    session.commit()

    create_app(settings)

    session.expire_all()
    run = session.query(JobRun).one()
    assert run.status == RunStatus.success
    assert run.summary is None
