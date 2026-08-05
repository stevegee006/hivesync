"""M1 acceptance criteria, against the real fixtures.

Runs inside the container, on the fixture network, so `rclone` is the pinned
binary rather than whatever is on a developer's PATH. Start with:

    make test-integration

Criteria covered here:

1. SFTP, FTP and SMB connections each test green.
2. A remote defined only in the mounted rclone.conf can be selected, tested and
   browsed, with no credentials written to the database.
4. A hash-less backend disables checksum comparison with a visible reason.

Criterion 3, no plaintext secret in /config or the logs, is covered by
tests/test_redaction_sweep.py, which needs no network.

These assert on resulting state rather than on log strings, per CLAUDE.md rule 4.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app import capabilities, probe
from app.crypto import SecretBox
from app.db import create_db_engine
from app.engines import inspect, rcloneconf
from app.models import Connection, ConnectionType, Credential, CredentialKind, RcloneMode
from tests.conftest import create_schema, make_settings

pytestmark = pytest.mark.integration

# Service names on the fixture network, overridable for a host-side run against
# the published ports.
SFTP_HOST = os.environ.get("HIVESYNC_TEST_SFTP_HOST", "sftp")
SFTP_PORT = int(os.environ.get("HIVESYNC_TEST_SFTP_PORT", "22"))
FTP_HOST = os.environ.get("HIVESYNC_TEST_FTP_HOST", "ftp")
FTP_PORT = int(os.environ.get("HIVESYNC_TEST_FTP_PORT", "21"))
SMB_HOST = os.environ.get("HIVESYNC_TEST_SMB_HOST", "smb")
SMB_PORT = int(os.environ.get("HIVESYNC_TEST_SMB_PORT", "445"))

USERNAME = "testuser"
PASSWORD = "testpass"


@pytest.fixture
def box(settings_for_integration) -> SecretBox:
    return SecretBox(settings_for_integration.secret_key)


@pytest.fixture
def settings_for_integration(tmp_path: Path):
    settings = make_settings(tmp_path)
    create_schema(settings)
    return settings


@pytest.fixture
def db(settings_for_integration):
    return sessionmaker(bind=create_db_engine(settings_for_integration))()


def _credential(db, box: SecretBox, name: str) -> Credential:
    credential = Credential(
        name=name,
        kind=CredentialKind.password,
        secret_ciphertext=box.encrypt(PASSWORD),
    )
    db.add(credential)
    db.commit()
    return credential


def _connect(db, box: SecretBox, **kwargs) -> Connection:  # noqa: ANN003
    credential = _credential(db, box, f"{kwargs['name']}-cred")
    connection = Connection(credential_id=credential.id, **kwargs)
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


# --------------------------------------------------------------------------
# Criterion 1: SFTP, FTP and SMB each test green
# --------------------------------------------------------------------------


def test_sftp_requires_host_key_trust_then_tests_green(db, box, settings_for_integration) -> None:
    """Trust on first use: the first test must not pass, the second must."""
    connection = _connect(
        db,
        box,
        name="fixture-sftp",
        type=ConnectionType.sftp,
        host=SFTP_HOST,
        port=SFTP_PORT,
        username=USERNAME,
        base_path="upload",
    )

    first = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert first.ok is False, "an unverified host key must not produce a green test"
    assert first.host_key is not None
    assert first.host_key.fingerprint.startswith(("ssh-", "ecdsa-"))
    assert "SHA256:" in first.host_key.fingerprint

    probe.trust_host_key(connection, first.host_key.fingerprint, session=db)
    assert connection.host_keys

    second = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert second.ok is True, second.message
    assert connection.last_test_ok is True
    caps = capabilities.for_connection(connection)
    assert caps.probed is True
    assert caps.can_set_modtime is True
    # Deliberately no assertion that SFTP exposes hashes. SPEC 11.1 lists md5 and
    # sha1 for SFTP, but that is a property of the server, not the protocol:
    # rclone detects them by running md5sum and sha1sum over a shell, and this
    # fixture is chroot SFTP-only with no shell, so it reports none. Exactly the
    # reason the capability probe exists instead of a static table.


def test_sftp_with_a_wrong_pinned_key_fails_loudly(db, box, settings_for_integration) -> None:
    """A changed host key is either a rebuilt server or an interception, and the
    message has to say so."""
    connection = _connect(
        db,
        box,
        name="fixture-sftp-badkey",
        type=ConnectionType.sftp,
        host=SFTP_HOST,
        port=SFTP_PORT,
        username=USERNAME,
        base_path="upload",
    )
    connection.host_keys = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    connection.host_keys_trusted = True
    db.commit()

    result = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert result.ok is False
    assert "host key" in result.message.lower()


def test_ftp_tests_green(db, box, settings_for_integration) -> None:
    connection = _connect(
        db,
        box,
        name="fixture-ftp",
        type=ConnectionType.ftp,
        host=FTP_HOST,
        port=FTP_PORT,
        username=USERNAME,
        base_path="",
    )
    result = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert result.ok is True, result.message
    caps = capabilities.for_connection(connection)
    assert caps.probed is True
    # SPEC 11.1: FTP exposes no hashes. This is the real-world check behind the
    # checksum reason string.
    assert caps.hashes == frozenset()


def test_smb_tests_green_and_reports_no_hashes(db, box, settings_for_integration) -> None:
    connection = _connect(
        db,
        box,
        name="fixture-smb",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username=USERNAME,
        share="testshare",
        base_path="",
    )
    result = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert result.ok is True, result.message
    caps = capabilities.for_connection(connection)
    assert caps.probed is True
    assert caps.hashes == frozenset(), "SPEC 11.1 says SMB exposes no hash types"
    # The forum claim that SMB cannot do modtimes is out of date. SPEC 11.1.
    assert caps.can_set_modtime is True
    assert caps.supports_move is True
    assert any("no hash types" in warning for warning in result.warnings)


def test_wrong_password_fails_with_an_auth_message(db, box, settings_for_integration) -> None:
    credential = Credential(
        name="wrong-pass",
        kind=CredentialKind.password,
        secret_ciphertext=box.encrypt("definitely-not-the-password"),
    )
    db.add(credential)
    db.commit()
    connection = Connection(
        name="fixture-smb-badpass",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username=USERNAME,
        share="testshare",
        base_path="",
        credential_id=credential.id,
    )
    db.add(connection)
    db.commit()

    result = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert result.ok is False
    assert connection.last_test_ok is False


# --------------------------------------------------------------------------
# Criterion 4, in the real world
# --------------------------------------------------------------------------


def test_real_smb_and_sftp_pairing_disables_checksum(db, box, settings_for_integration) -> None:
    """The acceptance criterion, using capabilities read from live endpoints
    rather than fixtures pasted into a test."""
    sftp = _connect(
        db,
        box,
        name="real-sftp",
        type=ConnectionType.sftp,
        host=SFTP_HOST,
        port=SFTP_PORT,
        username=USERNAME,
        base_path="upload",
    )
    first = probe.test_connection(sftp, box=box, settings=settings_for_integration, session=db)
    assert first.host_key is not None
    probe.trust_host_key(sftp, first.host_key.fingerprint, session=db)
    assert probe.test_connection(sftp, box=box, settings=settings_for_integration, session=db).ok

    smb = _connect(
        db,
        box,
        name="real-smb",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username=USERNAME,
        share="testshare",
        base_path="",
    )
    assert probe.test_connection(smb, box=box, settings=settings_for_integration, session=db).ok

    result = capabilities.intersect(sftp, smb)
    assert result.checksum.available is False
    assert result.checksum.reason is not None
    assert "no hash types" in result.checksum.reason
    assert "real-smb" in result.checksum.reason
    assert "modification time and size" in result.checksum.reason
    # Both write modtimes, so bidirectional stays available.
    assert result.bidirectional.available is True


# --------------------------------------------------------------------------
# Criterion 2: an imported remote works and stores no credentials
# --------------------------------------------------------------------------


def test_imported_remote_is_listed_tested_and_browsed(db, box, settings_for_integration) -> None:
    """A remote defined only in the mounted rclone.conf, with nothing of the
    user's config entering the database."""
    config_path = settings_for_integration.user_rclone_conf
    config_path.parent.mkdir(parents=True, exist_ok=True)
    obscured = rcloneconf.obscure(PASSWORD)
    config_path.write_text(
        "[fixture-imported]\n"
        "type = smb\n"
        f"host = {SMB_HOST}\n"
        f"port = {SMB_PORT}\n"
        f"user = {USERNAME}\n"
        f"pass = {obscured}\n",
        encoding="utf-8",
    )

    remotes = inspect.list_imported_remotes(config_path)
    assert "fixture-imported" in remotes

    connection = Connection(
        name="via-config",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.imported,
        rclone_remote_name="fixture-imported",
        base_path="testshare",
    )
    db.add(connection)
    db.commit()

    result = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert result.ok is True, result.message

    # Nothing of the user's credentials reached the database.
    assert connection.credential_id is None
    assert db.query(Credential).count() == 0

    entries = probe.browse(connection, None, box=box, settings=settings_for_integration)
    assert isinstance(entries, list)


def test_user_config_is_never_written(db, box, settings_for_integration) -> None:
    """SPEC 5.2: open read only, never write, never create."""
    config_path = settings_for_integration.user_rclone_conf
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "[fixture-imported]\ntype = smb\n"
        f"host = {SMB_HOST}\nport = {SMB_PORT}\nuser = {USERNAME}\n"
        f"pass = {rcloneconf.obscure(PASSWORD)}\n"
    )
    config_path.write_text(original, encoding="utf-8")

    connection = Connection(
        name="via-config-2",
        type=ConnectionType.rclone_remote,
        rclone_mode=RcloneMode.imported,
        rclone_remote_name="fixture-imported",
        base_path="testshare",
    )
    db.add(connection)
    db.commit()
    probe.test_connection(connection, box=box, settings=settings_for_integration, session=db)

    assert config_path.read_text(encoding="utf-8") == original


def test_missing_user_config_reports_clearly(settings_for_integration) -> None:
    with pytest.raises(inspect.InspectError, match="Mount your"):
        inspect.list_imported_remotes(settings_for_integration.user_rclone_conf)


# --------------------------------------------------------------------------
# Browsing and traversal against a real endpoint
# --------------------------------------------------------------------------


def test_browse_lists_a_real_directory(db, box, settings_for_integration) -> None:
    connection = _connect(
        db,
        box,
        name="browse-smb",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username=USERNAME,
        share="testshare",
        base_path="",
    )
    entries = probe.browse(connection, None, box=box, settings=settings_for_integration)
    assert isinstance(entries, list)


def test_browse_refuses_traversal_against_a_real_endpoint(
    db, box, settings_for_integration
) -> None:
    connection = _connect(
        db,
        box,
        name="traversal-smb",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username=USERNAME,
        share="testshare",
        base_path="",
    )
    with pytest.raises(rcloneconf.RemoteConfigError, match="outside the configured base path"):
        probe.browse(connection, "../../etc", box=box, settings=settings_for_integration)


# --------------------------------------------------------------------------
# The secret path, end to end against a real server
# --------------------------------------------------------------------------


def test_obscure_round_trip_through_a_real_login(db, box, settings_for_integration) -> None:
    """Proves the whole secret path: encrypted at rest, decrypted, obscured via
    stdin, passed through the environment, and accepted by a real server."""
    connection = _connect(
        db,
        box,
        name="secret-path-smb",
        type=ConnectionType.smb,
        host=SMB_HOST,
        port=SMB_PORT,
        username=USERNAME,
        share="testshare",
        base_path="",
    )
    result = probe.test_connection(
        connection, box=box, settings=settings_for_integration, session=db
    )
    assert result.ok is True, result.message
    # And the command that ran carries no credential, because it never had one.
    assert result.command is not None
    assert PASSWORD not in result.command
