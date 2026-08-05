"""Connection and credential API behaviour.

Validation is asserted on directly because SPEC section 6.4 asks the tool to fail
with a clear message rather than guess at intent, and "clear" is a property of the
string, not of the status code.

Nothing here reaches the network: rclone is absent on a developer workstation, so
the test and browse routes are exercised for their failure handling. The green
path against real endpoints is the integration suite's job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine
from app.models import Connection, Job
from tests.conftest import make_settings


def _sftp_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "prod-sftp",
        "type": "sftp",
        "host": "sftp.example.test",
        "port": 2222,
        "username": "svc",
        "base_path": "/var/www",
    }
    payload.update(overrides)
    return payload


def _create(client: TestClient, **overrides: object) -> dict:
    response = client.post("/api/connections", json=_sftp_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_create_and_read_back(authed_client: TestClient) -> None:
    created = _create(authed_client)
    assert created["name"] == "prod-sftp"
    assert created["capabilities"]["probed"] is False

    fetched = authed_client.get(f"/api/connections/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["host"] == "sftp.example.test"


def test_smb_without_a_share_is_refused_with_an_explanation(
    authed_client: TestClient,
) -> None:
    response = authed_client.post(
        "/api/connections",
        json={"name": "nas", "type": "smb", "host": "10.0.20.15", "base_path": "x"},
    )
    assert response.status_code == 422
    assert "share" in response.text
    # The message has to say why, not just that it is invalid.
    assert "remote:Share/path" in response.text


def test_share_on_a_non_smb_connection_is_refused(authed_client: TestClient) -> None:
    response = authed_client.post("/api/connections", json=_sftp_payload(share="Media"))
    assert response.status_code == 422
    assert "Only an SMB connection uses a share" in response.text


def test_network_type_without_a_host_is_refused(authed_client: TestClient) -> None:
    response = authed_client.post("/api/connections", json=_sftp_payload(host=None))
    assert response.status_code == 422
    assert "needs a host" in response.text


def test_local_without_a_base_path_is_refused(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/connections", json={"name": "disk", "type": "local", "base_path": ""}
    )
    assert response.status_code == 422
    assert "needs a base path" in response.text


def test_sentinel_file_only_applies_to_local(authed_client: TestClient) -> None:
    response = authed_client.post("/api/connections", json=_sftp_payload(sentinel_file=".mounted"))
    assert response.status_code == 422
    assert "only applies to a local connection" in response.text


def test_sentinel_file_accepted_on_local(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/connections",
        json={
            "name": "nas-mount",
            "type": "local",
            "base_path": "/data/media",
            "sentinel_file": ".hivesync-mounted",
        },
    )
    assert response.status_code == 201
    assert response.json()["sentinel_file"] == ".hivesync-mounted"


def test_rclone_remote_requires_a_mode(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/connections", json={"name": "adv", "type": "rclone_remote", "base_path": ""}
    )
    assert response.status_code == 422
    assert "needs a mode" in response.text


def test_inline_rclone_remote_requires_a_backend_type(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/connections",
        json={
            "name": "adv",
            "type": "rclone_remote",
            "rclone_mode": "inline",
            "base_path": "",
        },
    )
    assert response.status_code == 422
    assert "backend type" in response.text


def test_imported_rclone_remote_requires_a_remote_name(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/connections",
        json={
            "name": "adv",
            "type": "rclone_remote",
            "rclone_mode": "imported",
            "base_path": "",
        },
    )
    assert response.status_code == 422
    assert "mounted rclone.conf" in response.text


def test_imported_rclone_remote_stores_no_credential(authed_client: TestClient) -> None:
    """M1 criterion two: nothing from the user's config enters the database."""
    response = authed_client.post(
        "/api/connections",
        json={
            "name": "from-conf",
            "type": "rclone_remote",
            "rclone_mode": "imported",
            "rclone_remote_name": "mydrive",
            "base_path": "folder",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["credential_id"] is None
    assert body["rclone_remote_name"] == "mydrive"


def test_mode_on_a_native_type_is_refused(authed_client: TestClient) -> None:
    response = authed_client.post("/api/connections", json=_sftp_payload(rclone_mode="inline"))
    assert response.status_code == 422
    assert "only applies to an rclone remote" in response.text


def test_duplicate_name_is_a_conflict(authed_client: TestClient) -> None:
    _create(authed_client)
    response = authed_client.post("/api/connections", json=_sftp_payload())
    assert response.status_code == 409


# --------------------------------------------------------------------------
# Update, delete, and probe invalidation
# --------------------------------------------------------------------------


def test_editing_the_endpoint_identity_clears_the_probe(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    """SPEC 5.4 says re-probe on edit. A stale probe that permits an option is
    worse than an unprobed one that blocks it."""
    created = _create(authed_client)

    settings = make_settings(app_settings_path)
    session = sessionmaker(bind=create_db_engine(settings))()
    connection = session.get(Connection, created["id"])
    assert connection is not None
    connection.capabilities = {"Precision": 1, "Hashes": ["md5"], "Features": {"Move": True}}
    from app.models import utcnow

    connection.capabilities_probed_at = utcnow()
    session.commit()

    assert authed_client.get(f"/api/connections/{created['id']}").json()["capabilities"]["probed"]

    response = authed_client.patch(
        f"/api/connections/{created['id']}", json=_sftp_payload(host="different.example.test")
    )
    assert response.status_code == 200
    assert response.json()["capabilities"]["probed"] is False


def test_editing_an_unrelated_field_keeps_the_probe(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    created = _create(authed_client)
    settings = make_settings(app_settings_path)
    session = sessionmaker(bind=create_db_engine(settings))()
    connection = session.get(Connection, created["id"])
    assert connection is not None
    from app.models import utcnow

    connection.capabilities = {"Precision": 1, "Hashes": ["md5"], "Features": {}}
    connection.capabilities_probed_at = utcnow()
    session.commit()

    response = authed_client.patch(
        f"/api/connections/{created['id']}", json=_sftp_payload(base_path="/srv/other")
    )
    assert response.status_code == 200
    assert response.json()["capabilities"]["probed"] is True


def test_delete_blocked_while_a_job_uses_it(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    source = _create(authed_client, name="src")
    dest = _create(authed_client, name="dst")

    settings = make_settings(app_settings_path)
    session = sessionmaker(bind=create_db_engine(settings))()
    session.add(
        Job(
            name="nightly",
            source_connection_id=source["id"],
            source_path="a",
            dest_connection_id=dest["id"],
            dest_path="b",
        )
    )
    session.commit()

    response = authed_client.delete(f"/api/connections/{source['id']}")
    assert response.status_code == 409
    assert "nightly" in response.text


def test_delete_works_when_unused(authed_client: TestClient) -> None:
    created = _create(authed_client)
    assert authed_client.delete(f"/api/connections/{created['id']}").status_code == 204
    assert authed_client.get(f"/api/connections/{created['id']}").status_code == 404


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/connections"),
        ("post", "/api/connections"),
        ("get", "/api/credentials"),
        ("post", "/api/credentials"),
        ("get", "/api/rclone/backends"),
        ("get", "/api/rclone/remotes"),
    ],
)
def test_endpoints_require_authentication(client: TestClient, method: str, path: str) -> None:
    """Auth is a dependency, so it runs before body validation. An unauthenticated
    caller gets a flat 401 rather than a 422 describing a payload they were never
    entitled to submit."""
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Test and browse failure handling
# --------------------------------------------------------------------------


def test_test_endpoint_reports_a_failure_rather_than_raising(
    authed_client: TestClient,
) -> None:
    """rclone is absent here, so this exercises the path where the probe cannot
    run. An unreachable endpoint is a result to read, not a 500."""
    created = _create(authed_client, type="ftp", port=21)
    response = authed_client.post(f"/api/connections/{created['id']}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["message"]


def test_failed_test_is_recorded_on_the_connection(authed_client: TestClient) -> None:
    created = _create(authed_client, type="ftp", port=21)
    authed_client.post(f"/api/connections/{created['id']}/test")
    fetched = authed_client.get(f"/api/connections/{created['id']}").json()
    assert fetched["last_test_ok"] is False
    assert fetched["last_test_at"] is not None
    assert fetched["last_test_error"]


def test_browse_rejects_path_traversal(authed_client: TestClient) -> None:
    """A directory picker that can walk out of its base path is a bug."""
    created = _create(authed_client)
    response = authed_client.get(
        f"/api/connections/{created['id']}/browse", params={"path": "../../etc"}
    )
    assert response.status_code == 400
    assert "outside the configured base path" in response.text


def test_trust_host_key_requires_a_fingerprint(authed_client: TestClient) -> None:
    created = _create(authed_client)
    response = authed_client.post(
        f"/api/connections/{created['id']}/trust-host-key", json={"fingerprint": ""}
    )
    assert response.status_code == 422


def test_forget_host_key_clears_it(authed_client: TestClient, app_settings_path: Path) -> None:
    created = _create(authed_client)
    settings = make_settings(app_settings_path)
    session = sessionmaker(bind=create_db_engine(settings))()
    connection = session.get(Connection, created["id"])
    assert connection is not None
    connection.host_key_fingerprint = "ssh-ed25519 AAAAC3Nz"
    session.commit()

    response = authed_client.delete(f"/api/connections/{created['id']}/host-key")
    assert response.status_code == 200
    assert response.json()["host_key_fingerprint"] is None


# --------------------------------------------------------------------------
# Compatibility endpoint, M1 criterion four
# --------------------------------------------------------------------------


def _probe_connection(
    authed_client: TestClient, app_settings_path: Path, name: str, probe: dict
) -> int:
    created = _create(authed_client, name=name)
    settings = make_settings(app_settings_path)
    session = sessionmaker(bind=create_db_engine(settings))()
    connection = session.get(Connection, created["id"])
    assert connection is not None
    from app.models import utcnow

    connection.capabilities = probe
    connection.capabilities_probed_at = utcnow()
    session.commit()
    return int(created["id"])


def test_compatibility_reports_the_hashless_reason(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    sftp_id = _probe_connection(
        authed_client,
        app_settings_path,
        "prod-sftp",
        {"Precision": 1, "Hashes": ["md5", "sha1"], "Features": {"Move": True}},
    )
    smb_id = _probe_connection(
        authed_client,
        app_settings_path,
        "synology",
        {"Precision": 1, "Hashes": [], "Features": {"Move": True}},
    )

    response = authed_client.get(
        "/api/rclone/compatibility", params={"source_id": sftp_id, "dest_id": smb_id}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["checksum"]["available"] is False
    assert "no hash types" in body["checksum"]["reason"]
    assert "synology" in body["checksum"]["reason"]


def test_compatibility_404s_on_a_missing_connection(authed_client: TestClient) -> None:
    created = _create(authed_client)
    response = authed_client.get(
        "/api/rclone/compatibility", params={"source_id": created["id"], "dest_id": 9999}
    )
    assert response.status_code == 404
