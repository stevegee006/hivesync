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
