"""M1 acceptance criterion three: no plaintext secret in /config or the logs.

SPEC section 17 asks for a test that greps every log file and every database text
column for known test secrets. This is that test, scoped to what M1 can produce.
It grows as later milestones write run logs.

The sentinel values are deliberately distinctive so a substring search cannot
match them by accident.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.crypto import SecretBox
from app.db import create_db_engine
from app.models import Connection, ConnectionType, Credential, CredentialKind
from tests.conftest import create_schema, make_settings

SENTINEL_PASSWORD = "ZZ-sentinel-password-must-never-appear-42"
SENTINEL_PEM = "ZZ-sentinel-private-key-must-never-appear-43"
SENTINEL_PASSPHRASE = "ZZ-sentinel-passphrase-must-never-appear-44"


def _create_credentials(client: TestClient) -> list[int]:
    ids = []
    response = client.post(
        "/api/credentials",
        json={
            "name": "sweep-password",
            "kind": "password",
            "secret": SENTINEL_PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    ids.append(response.json()["id"])

    response = client.post(
        "/api/credentials",
        json={
            "name": "sweep-key",
            "kind": "ssh_key",
            "secret": SENTINEL_PEM,
            "key_passphrase": SENTINEL_PASSPHRASE,
        },
    )
    assert response.status_code == 201, response.text
    ids.append(response.json()["id"])
    return ids


def test_create_response_never_echoes_the_secret(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/credentials",
        json={"name": "echo-check", "kind": "password", "secret": SENTINEL_PASSWORD},
    )
    assert response.status_code == 201
    assert SENTINEL_PASSWORD not in response.text
    assert "secret" not in response.json()


def test_list_response_never_includes_secrets(authed_client: TestClient) -> None:
    _create_credentials(authed_client)
    response = authed_client.get("/api/credentials")
    assert response.status_code == 200
    assert SENTINEL_PASSWORD not in response.text
    assert SENTINEL_PEM not in response.text
    assert SENTINEL_PASSPHRASE not in response.text
    for row in response.json():
        assert "secret" not in row
        assert "secret_ciphertext" not in row


def test_no_plaintext_secret_in_any_database_text_column(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    """Sweep every text column of every table, which is what SPEC 17 asks for."""
    _create_credentials(authed_client)

    settings = make_settings(app_settings_path)
    engine = create_db_engine(settings)
    findings: list[str] = []
    with engine.connect() as connection:
        tables = [
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [
                row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
            ]
            for column in columns:
                rows = connection.execute(
                    text(f'SELECT CAST("{column}" AS TEXT) FROM "{table}"')  # noqa: S608
                ).scalars()
                for value in rows:
                    if value is None:
                        continue
                    for sentinel in (SENTINEL_PASSWORD, SENTINEL_PEM, SENTINEL_PASSPHRASE):
                        if sentinel in str(value):
                            findings.append(f"{table}.{column}")
    assert not findings, f"Plaintext secret found in: {sorted(set(findings))}"


def test_no_plaintext_secret_anywhere_under_config(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    """Walk the whole /config tree, not just the files we expect to exist."""
    _create_credentials(authed_client)

    offenders: list[str] = []
    for path in app_settings_path.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        for sentinel in (SENTINEL_PASSWORD, SENTINEL_PEM, SENTINEL_PASSPHRASE):
            if sentinel.encode() in blob:
                offenders.append(str(path.relative_to(app_settings_path)))
    assert not offenders, f"Plaintext secret found in files: {sorted(set(offenders))}"


def test_no_plaintext_secret_in_log_records(authed_client: TestClient) -> None:
    """Credential routes log names and kinds. They must never log a value.

    caplog is not used: app.logging_conf clears the root handlers when the app is
    built, which removes pytest's capture handler along with everything else. The
    handler is attached here, after the app exists, so it survives.
    """
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collector(level=logging.DEBUG)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        credential_ids = _create_credentials(authed_client)
        authed_client.patch(
            f"/api/credentials/{credential_ids[0]}",
            json={"secret": SENTINEL_PASSWORD + "-rotated"},
        )
        authed_client.get("/api/credentials")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    captured = "\n".join(
        [record.getMessage() for record in records] + [str(record.__dict__) for record in records]
    )
    assert SENTINEL_PASSWORD not in captured
    assert SENTINEL_PEM not in captured
    assert SENTINEL_PASSPHRASE not in captured
    # The name is expected in the log, so this proves the sweep is looking at
    # real content rather than passing on an empty capture.
    assert "sweep-password" in captured


def test_ciphertext_actually_differs_from_plaintext(tmp_path: Path) -> None:
    """Guards against a future change storing the value unencrypted by accident."""
    settings = make_settings(tmp_path)
    create_schema(settings)
    engine = create_db_engine(settings)
    session = sessionmaker(bind=engine)()

    box = SecretBox(settings.secret_key)
    session.add(
        Credential(
            name="direct-write",
            kind=CredentialKind.password,
            secret_ciphertext=box.encrypt(SENTINEL_PASSWORD),
        )
    )
    session.commit()

    raw = session.execute(
        text("SELECT secret_ciphertext FROM credential WHERE name = 'direct-write'")
    ).scalar_one()
    assert SENTINEL_PASSWORD.encode() not in raw
    assert box.decrypt(raw) == SENTINEL_PASSWORD


def test_connection_read_never_exposes_credential_material(
    authed_client: TestClient, app_settings_path: Path
) -> None:
    credential_ids = _create_credentials(authed_client)
    settings = make_settings(app_settings_path)
    engine = create_db_engine(settings)
    session = sessionmaker(bind=engine)()
    session.add(
        Connection(
            name="sweep-conn",
            type=ConnectionType.sftp,
            host="example.test",
            base_path="/srv",
            credential_id=credential_ids[0],
        )
    )
    session.commit()

    response = authed_client.get("/api/connections")
    assert response.status_code == 200
    assert SENTINEL_PASSWORD not in response.text
    # It may reference the credential by id, which is not secret.
    assert "credential_id" in response.text


# --------------------------------------------------------------------------
# M7 surfaces, swept at M8
# --------------------------------------------------------------------------

SENTINEL_WEBHOOK_TOKEN = "ZZ-sentinel-webhook-token-must-never-appear-45"


def test_the_config_export_carries_no_sentinel(authed_client: TestClient) -> None:
    """The export is a file people put in a git repository or paste into a
    support thread, which is why it excludes ciphertext as well as plaintext."""
    _create_credentials(authed_client)
    authed_client.patch(
        "/api/settings",
        json={
            "notify_target": "webhook",
            "notify_webhook_url": f"https://example.invalid/hook/{SENTINEL_WEBHOOK_TOKEN}",
        },
    )

    body = authed_client.get("/settings/export").text

    for sentinel in (
        SENTINEL_PASSWORD,
        SENTINEL_PEM,
        SENTINEL_PASSPHRASE,
        SENTINEL_WEBHOOK_TOKEN,
    ):
        assert sentinel not in body


def test_the_settings_page_never_renders_the_webhook_token(authed_client: TestClient) -> None:
    authed_client.patch(
        "/api/settings",
        json={
            "notify_target": "webhook",
            "notify_webhook_url": f"https://example.invalid/hook/{SENTINEL_WEBHOOK_TOKEN}",
        },
    )
    assert SENTINEL_WEBHOOK_TOKEN not in authed_client.get("/settings").text
    assert SENTINEL_WEBHOOK_TOKEN not in authed_client.get("/api/settings").text


def test_a_failed_notification_does_not_log_the_url(
    authed_client: TestClient, caplog: object
) -> None:
    """The failure message reaches an operator, and a URL that carries a token in
    its path must not travel with it."""
    import logging as _logging

    authed_client.patch(
        "/api/settings",
        json={
            "notify_target": "webhook",
            # Port 9 discards, so this fails without waiting on a real endpoint.
            "notify_webhook_url": f"http://127.0.0.1:9/hook/{SENTINEL_WEBHOOK_TOKEN}",
            "notify_timeout_seconds": 1,
        },
    )

    with caplog.at_level(_logging.DEBUG):  # type: ignore[attr-defined]
        result = authed_client.post("/api/settings/test-notification").json()

    assert result["ok"] is False
    assert SENTINEL_WEBHOOK_TOKEN not in result["detail"]
    for record in caplog.records:  # type: ignore[attr-defined]
        assert SENTINEL_WEBHOOK_TOKEN not in record.getMessage()
        assert SENTINEL_WEBHOOK_TOKEN not in str(record.__dict__)


def test_metrics_carries_no_sentinel(authed_client: TestClient) -> None:
    """Job names reach the metric labels, so a job named after a secret would
    export it. Nothing else should get there at all."""
    _create_credentials(authed_client)
    assert SENTINEL_PASSWORD not in authed_client.get("/metrics").text


def test_the_api_token_is_never_echoed(tmp_path: Path) -> None:
    """It authenticates every request that carries it, so it must not appear in
    any response, including the settings screen that mentions the metrics one."""
    from app.main import create_app

    sentinel = "ZZ-sentinel-api-token-must-never-appear-46"
    settings = make_settings(tmp_path, api_token=sentinel, metrics_token=sentinel)
    create_schema(settings)
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {sentinel}"}
        for path in ("/settings", "/api/settings", "/api/health", "/metrics"):
            assert sentinel not in client.get(path, headers=headers).text
