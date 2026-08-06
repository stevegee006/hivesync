"""Configuration export and import.

Acceptance criterion: an export imports into an empty instance reproducing every
connection, job and preset, with zero Credential rows and no secret anywhere in
the file.

The secret sweep is the important test here. It greps the serialised document for
the plaintext, the ciphertext and the obscured form, because "we did not include
the password" and "the password is not in the file" are different claims and only
the second one matters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import portable
from app import preferences as preferences_store
from app.config import Settings
from app.crypto import SecretBox
from app.db import create_db_engine
from app.models import (
    CompareMode,
    Connection,
    ConnectionType,
    Credential,
    CredentialKind,
    DeleteMode,
    Direction,
    FilterPreset,
    Job,
    NotifyOn,
)
from app.preferences import Preferences
from tests.conftest import create_schema, make_settings

PASSWORD = "correct-horse-battery-staple"


def _session(settings: Settings) -> Session:
    create_schema(settings)
    return sessionmaker(bind=create_db_engine(settings))()


@pytest.fixture
def source(settings: Settings):
    """A populated instance: credentials, connections, a job and a preset."""
    session = _session(settings)
    box = SecretBox(settings.secret_key)
    credential = Credential(
        name="nas-login", kind=CredentialKind.password, secret_ciphertext=box.encrypt(PASSWORD)
    )
    session.add(credential)
    session.commit()

    local = Connection(name="staging", type=ConnectionType.local, base_path="/data/staging")
    nas = Connection(
        name="synology",
        type=ConnectionType.smb,
        host="nas.local",
        port=445,
        share="Media",
        base_path="photos",
        username="steve",
        credential_id=credential.id,
        # Probe output, which describes an environment rather than a
        # configuration and must not travel.
        capabilities={"Hashes": ["md5"]},
        host_keys="ssh-ed25519 AAAA",
        host_keys_trusted=True,
    )
    session.add_all([local, nas])
    session.commit()

    preset = FilterPreset(name="My junk", builtin=False, rules={"exclude": ["**/*.tmp"]})
    session.add(preset)
    session.commit()

    job = Job(
        name="Nightly Media",
        source_connection_id=local.id,
        dest_connection_id=nas.id,
        source_path="incoming",
        dest_path="",
        direction=Direction.source_to_dest,
        delete_mode=DeleteMode.archive,
        archive_base="/data/attic",
        compare_mode=CompareMode.size_only,
        max_delete_pct=15,
        schedule_cron="30 2 * * *",
        timezone="America/Denver",
        notify_on=NotifyOn.always,
        filters={"exclude": ["*.partial"]},
    )
    job.filter_presets.append(preset)
    session.add(job)
    session.commit()

    preferences_store.save(
        session,
        Preferences(
            notify_target="webhook",
            notify_webhook_url="https://hooks.example.invalid/t/SUPER-SECRET-TOKEN",
            archive_retention_days=45,
            run_history_keep=500,
        ),
    )
    return session


@pytest.fixture
def empty(tmp_path: Path):
    """A fresh instance with its own database and its own key."""
    settings = make_settings(tmp_path / "fresh")
    return _session(settings)


# --------------------------------------------------------------------------
# Nothing secret leaves
# --------------------------------------------------------------------------


def test_the_export_contains_no_secret_in_any_form(source, settings: Settings) -> None:
    box = SecretBox(settings.secret_key)
    ciphertext = source.get(Credential, 1).secret_ciphertext
    stored = ciphertext.decode("utf-8", "replace") if isinstance(ciphertext, bytes) else ciphertext

    document = json.dumps(portable.export(source))

    assert PASSWORD not in document
    assert stored not in document
    assert box.decrypt(ciphertext) == PASSWORD, "the credential itself is untouched"
    # And the webhook URL, which can carry its own token.
    assert "SUPER-SECRET-TOKEN" not in document


def test_the_export_names_what_must_be_re_entered(source) -> None:
    """A list of names is the difference between an import that looks complete
    and one someone can actually finish."""
    document = portable.export(source)
    assert document["credentials_required"] == [{"name": "nas-login", "kind": "password"}]
    assert "no credentials" in document["note"].lower()


def test_probe_output_is_not_exported(source) -> None:
    """Capabilities and host keys describe an environment, and a host key is a
    trust decision that has to be made on the machine doing the trusting."""
    document = json.dumps(portable.export(source))
    assert "host_keys" not in document
    assert "capabilities" not in document
    assert "ssh-ed25519" not in document


# --------------------------------------------------------------------------
# The criterion: a round trip into an empty instance
# --------------------------------------------------------------------------


def test_a_round_trip_reproduces_connections_jobs_and_presets(source, empty) -> None:
    document = portable.export(source)

    report = portable.import_document(empty, document)

    assert report.ok, report.errors
    assert report.connections_created == 2
    assert report.jobs_created == 1
    assert report.presets_created == 1
    # No credential row was created, because none was exported.
    assert empty.query(Credential).count() == 0

    nas = empty.query(Connection).filter_by(name="synology").one()
    assert nas.type == ConnectionType.smb
    assert nas.host == "nas.local"
    assert nas.port == 445
    assert nas.share == "Media"
    assert nas.base_path == "photos"
    assert nas.username == "steve"
    assert nas.credential_id is None

    job = empty.query(Job).one()
    assert job.name == "Nightly Media"
    assert job.source_connection.name == "staging"
    assert job.dest_connection.name == "synology"
    assert job.delete_mode == DeleteMode.archive
    assert job.archive_base == "/data/attic"
    assert job.compare_mode == CompareMode.size_only
    assert job.max_delete_pct == 15
    assert job.schedule_cron == "30 2 * * *"
    assert job.timezone == "America/Denver"
    assert job.notify_on == NotifyOn.always
    assert job.filters == {"exclude": ["*.partial"]}
    assert [preset.name for preset in job.filter_presets] == ["My junk"]


def test_a_missing_credential_is_reported_rather_than_hidden(source, empty) -> None:
    report = portable.import_document(empty, portable.export(source))
    assert any("nas-login" in warning for warning in report.warnings)
    assert any("never contain secrets" in warning for warning in report.warnings)


def test_an_existing_credential_is_relinked_by_name(source, empty, tmp_path: Path) -> None:
    """Someone who re-enters their secrets first gets a working import."""
    fresh_box = SecretBox(make_settings(tmp_path / "fresh").secret_key)
    empty.add(
        Credential(
            name="nas-login",
            kind=CredentialKind.password,
            secret_ciphertext=fresh_box.encrypt("re-entered-by-hand"),
        )
    )
    empty.commit()

    report = portable.import_document(empty, portable.export(source))

    assert not report.warnings, report.warnings
    nas = empty.query(Connection).filter_by(name="synology").one()
    assert nas.credential is not None
    assert nas.credential.name == "nas-login"


def test_preferences_travel_except_the_deployment_specific_ones(source, empty) -> None:
    report = portable.import_document(empty, portable.export(source))
    assert report.preferences_applied is True
    imported = preferences_store.load(empty)
    assert imported.archive_retention_days == 45
    assert imported.run_history_keep == 500
    # Never the URL: it was not in the file at all.
    assert imported.notify_webhook_url == ""


# --------------------------------------------------------------------------
# Refusing to make a mess
# --------------------------------------------------------------------------


def test_importing_twice_changes_nothing_the_second_time(source, empty) -> None:
    document = portable.export(source)
    portable.import_document(empty, document)

    second = portable.import_document(empty, document)

    assert second.connections_created == 0
    assert second.jobs_created == 0
    assert second.connections_skipped == 2
    assert second.jobs_skipped == 1
    assert empty.query(Connection).count() == 2
    assert empty.query(Job).count() == 1


def test_an_existing_job_is_never_overwritten(source, empty) -> None:
    """An import that silently rewrites a working job is an import nobody can
    safely run twice."""
    document = portable.export(source)
    portable.import_document(empty, document)
    job = empty.query(Job).one()
    job.max_delete_pct = 1
    empty.commit()

    portable.import_document(empty, document)

    assert empty.query(Job).one().max_delete_pct == 1


def test_a_job_whose_endpoint_is_missing_is_refused_with_the_name(source, empty) -> None:
    document = portable.export(source)
    document["connections"] = [
        entry for entry in document["connections"] if entry["name"] != "synology"
    ]

    report = portable.import_document(empty, document)

    assert report.ok is False
    assert any("synology" in error for error in report.errors)
    # The rest still imported, so one bad row does not cost the whole file.
    assert report.connections_created == 1
    assert empty.query(Job).count() == 0


def test_a_file_from_a_future_version_is_refused_whole(source, empty) -> None:
    document = portable.export(source)
    document["format_version"] = 99

    report = portable.import_document(empty, document)

    assert report.ok is False
    assert "Nothing was imported" in report.errors[0]
    assert empty.query(Connection).count() == 0


def test_a_missing_preset_is_a_warning_that_says_what_changes(source, empty) -> None:
    document = portable.export(source)
    document["filter_presets"] = []

    report = portable.import_document(empty, document)

    assert report.jobs_created == 1
    assert any("My junk" in warning for warning in report.warnings)
    assert any("would have excluded" in warning for warning in report.warnings)
