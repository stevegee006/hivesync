"""The Settings screen, the preset editor, and the notification wiring.

Two things here are about not leaking rather than about features:

- The stored webhook URL is never rendered back into the page, and saving any
  other field must not wipe it.
- The export served by the web route carries no secret, tested through the HTTP
  layer rather than only through app.portable.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import preferences as preferences_store
from app.config import Settings
from app.db import create_db_engine
from app.models import Credential, CredentialKind, FilterPreset, Job
from app.preferences import Preferences
from tests.conftest import create_schema

WEBHOOK = "https://hooks.example.invalid/t/SECRET-TOKEN-VALUE"


def _session(settings: Settings) -> Session:
    return sessionmaker(bind=create_db_engine(settings))()


def _fresh_session(settings: Settings) -> Session:
    """For tests that do not go through the app fixture, which builds the schema."""
    create_schema(settings)
    return _session(settings)


def _form(**overrides: str) -> dict[str, str]:
    fields = {
        "notify_target": "none",
        "notify_ntfy_server": "https://ntfy.sh",
        "notify_ntfy_topic": "",
        "notify_timeout_seconds": "10",
        "base_url": "",
        "archive_retention_days": "",
        "run_history_keep": "200",
        "log_retention_days": "90",
        "log_max_total_mb": "512",
    }
    fields.update(overrides)
    return fields


# --------------------------------------------------------------------------
# Saving preferences
# --------------------------------------------------------------------------


def test_settings_save_and_come_back(authed_client: TestClient, settings: Settings) -> None:
    response = authed_client.post(
        "/settings",
        data=_form(
            notify_target="webhook",
            notify_webhook_url=WEBHOOK,
            base_url="http://nas.local:8080",
            archive_retention_days="45",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303

    stored = preferences_store.load(_session(settings))
    assert stored.notify_target == "webhook"
    assert stored.notify_webhook_url == WEBHOOK
    assert stored.base_url == "http://nas.local:8080"
    assert stored.archive_retention_days == 45


def test_the_stored_webhook_url_is_never_rendered(
    authed_client: TestClient, settings: Settings
) -> None:
    """It routinely carries a token in its path. A page that shows one hands it
    to anyone who can see the screen."""
    authed_client.post(
        "/settings",
        data=_form(notify_target="webhook", notify_webhook_url=WEBHOOK),
        follow_redirects=False,
    )

    page = authed_client.get("/settings").text

    assert "SECRET-TOKEN-VALUE" not in page
    assert WEBHOOK not in page
    # But it says one is stored, or nobody can tell it is configured.
    assert "A URL is stored" in page


def test_saving_another_field_keeps_the_stored_url(
    authed_client: TestClient, settings: Settings
) -> None:
    """The URL is not rendered back into the form, so an empty box has to mean
    "keep it". Otherwise every save of this page silently disconnects
    notifications."""
    authed_client.post(
        "/settings",
        data=_form(notify_target="webhook", notify_webhook_url=WEBHOOK),
        follow_redirects=False,
    )

    authed_client.post(
        "/settings",
        data=_form(notify_target="webhook", notify_webhook_url="", run_history_keep="300"),
        follow_redirects=False,
    )

    stored = preferences_store.load(_session(settings))
    assert stored.notify_webhook_url == WEBHOOK
    assert stored.run_history_keep == 300


def test_removing_the_url_is_an_explicit_action(
    authed_client: TestClient, settings: Settings
) -> None:
    authed_client.post(
        "/settings",
        data=_form(notify_target="webhook", notify_webhook_url=WEBHOOK),
        follow_redirects=False,
    )

    authed_client.post(
        "/settings",
        data=_form(notify_target="webhook", clear_webhook_url="true"),
        follow_redirects=False,
    )

    assert preferences_store.load(_session(settings)).notify_webhook_url == ""


def test_clearing_retention_turns_pruning_off(
    authed_client: TestClient, settings: Settings
) -> None:
    """Blank must mean never, not "keep the old number". This one deletes files."""
    authed_client.post("/settings", data=_form(archive_retention_days="30"))
    assert preferences_store.load(_session(settings)).archive_retention_days == 30

    authed_client.post("/settings", data=_form(archive_retention_days=""))
    assert preferences_store.load(_session(settings)).archive_retention_days is None


def test_a_bad_value_is_rejected_rather_than_stored(
    authed_client: TestClient, settings: Settings
) -> None:
    response = authed_client.post("/settings", data=_form(notify_timeout_seconds="999"))
    assert response.status_code == 400
    assert preferences_store.load(_session(settings)).notify_timeout_seconds == 10


def test_settings_needs_a_session(client: TestClient) -> None:
    assert client.get("/settings", follow_redirects=False).status_code == 303


# --------------------------------------------------------------------------
# The API surface
# --------------------------------------------------------------------------


def test_the_api_never_returns_the_url_either(authed_client: TestClient) -> None:
    authed_client.patch(
        "/api/settings", json={"notify_target": "webhook", "notify_webhook_url": WEBHOOK}
    )

    body = authed_client.get("/api/settings").json()

    assert body["notify_webhook_configured"] is True
    assert "notify_webhook_url" not in body
    assert "SECRET-TOKEN-VALUE" not in json.dumps(body)


def test_test_notification_reports_instead_of_failing(authed_client: TestClient) -> None:
    """With nothing configured it is a skip, not a 500."""
    body = authed_client.post("/api/settings/test-notification").json()
    assert body["attempted"] is False
    assert body["ok"] is False
    assert "No notification target" in body["detail"]


# --------------------------------------------------------------------------
# Export and import through the web layer
# --------------------------------------------------------------------------


def test_the_downloaded_export_carries_no_secret(
    authed_client: TestClient, settings: Settings
) -> None:
    session = _session(settings)
    from app.crypto import SecretBox

    session.add(
        Credential(
            name="nas",
            kind=CredentialKind.password,
            secret_ciphertext=SecretBox(settings.secret_key).encrypt("hunter2-the-password"),
        )
    )
    session.commit()

    response = authed_client.get("/settings/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "hunter2-the-password" not in response.text
    assert response.json()["credentials_required"] == [{"name": "nas", "kind": "password"}]


def test_importing_a_file_that_is_not_json_says_so(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/settings/import",
        files={"document": ("config.json", b"not json at all", "application/json")},
    )
    assert response.status_code == 400
    assert "not valid JSON" in response.text


def test_importing_an_export_creates_the_configuration(
    authed_client: TestClient, settings: Settings
) -> None:
    document = {
        "format_version": 1,
        "connections": [
            {"name": "a", "type": "local", "base_path": "/a"},
            {"name": "b", "type": "local", "base_path": "/b"},
        ],
        "jobs": [
            {
                "name": "Imported",
                "source_connection": "a",
                "dest_connection": "b",
                "max_delete_pct": 20,
            }
        ],
        "filter_presets": [],
        "preferences": {},
    }
    response = authed_client.post(
        "/settings/import",
        files={"document": ("config.json", json.dumps(document).encode(), "application/json")},
    )
    assert response.status_code == 200

    session = _session(settings)
    assert session.query(Job).filter_by(name="Imported").one().max_delete_pct == 20


# --------------------------------------------------------------------------
# Filter presets
# --------------------------------------------------------------------------


def test_presets_page_lists_the_builtins(authed_client: TestClient) -> None:
    page = authed_client.get("/filter-presets").text
    assert "Synology / DSM" in page
    assert "@eaDir" in page


def test_a_preset_can_be_created_and_edited(authed_client: TestClient, settings: Settings) -> None:
    authed_client.post(
        "/filter-presets",
        data={"name": "My rules", "exclude": "*.tmp\n*.partial", "include": ""},
        follow_redirects=False,
    )
    session = _session(settings)
    preset = session.query(FilterPreset).filter_by(name="My rules").one()
    assert preset.rules["exclude"] == ["*.tmp", "*.partial"]
    assert preset.builtin is False

    authed_client.post(
        "/filter-presets",
        data={
            "preset_id": str(preset.id),
            "name": "My rules",
            "exclude": "*.tmp",
            "include": "keep/**",
        },
        follow_redirects=False,
    )
    session.expire_all()
    updated = session.query(FilterPreset).filter_by(name="My rules").one()
    assert updated.rules == {"exclude": ["*.tmp"], "include": ["keep/**"]}


def test_a_builtin_preset_cannot_be_edited(authed_client: TestClient, settings: Settings) -> None:
    """It is re-seeded from the application at every startup, so an edit here
    would be silently undone by the next restart."""
    session = _session(settings)
    builtin = session.query(FilterPreset).filter_by(builtin=True).first()
    assert builtin is not None

    response = authed_client.post(
        "/filter-presets",
        data={"preset_id": str(builtin.id), "name": "Hijacked", "exclude": "", "include": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("error=builtin")

    session.expire_all()
    assert session.get(FilterPreset, builtin.id).name != "Hijacked"


def test_a_preset_in_use_cannot_be_deleted(authed_client: TestClient, settings: Settings) -> None:
    from app.models import Connection, ConnectionType

    session = _session(settings)
    preset = FilterPreset(name="Used", builtin=False, rules={"exclude": ["*.tmp"]})
    source = Connection(name="s", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="d", type=ConnectionType.local, base_path="/d")
    session.add_all([preset, source, dest])
    session.commit()
    job = Job(
        name="Uses it",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
    )
    job.filter_presets.append(preset)
    session.add(job)
    session.commit()
    preset_id = preset.id

    response = authed_client.post(f"/filter-presets/{preset_id}/delete", follow_redirects=False)

    assert response.headers["location"].endswith("error=in-use")
    session.expire_all()
    assert session.get(FilterPreset, preset_id) is not None
    # And the reason reaches the reader.
    assert "used by a job" in authed_client.get("/filter-presets?error=in-use").text


def test_an_unused_preset_can_be_deleted(authed_client: TestClient, settings: Settings) -> None:
    session = _session(settings)
    preset = FilterPreset(name="Spare", builtin=False, rules={})
    session.add(preset)
    session.commit()
    preset_id = preset.id

    authed_client.post(f"/filter-presets/{preset_id}/delete", follow_redirects=False)

    session.expire_all()
    assert session.get(FilterPreset, preset_id) is None


# --------------------------------------------------------------------------
# Preferences storage
# --------------------------------------------------------------------------


def test_a_corrupt_preference_row_falls_back_to_defaults(settings: Settings) -> None:
    """One bad row must not take down the scheduler and the settings screen that
    would let someone fix it."""
    from app.models import Setting

    session = _fresh_session(settings)
    preferences_store.save(session, Preferences(run_history_keep=500))
    corrupted = session.get(Setting, "pref.notify_timeout_seconds")
    assert corrupted is not None
    corrupted.value = "not-a-number"
    session.commit()

    loaded = preferences_store.load(session)

    assert loaded == Preferences()


def test_an_unknown_preference_row_is_ignored(settings: Settings) -> None:
    from app.models import Setting

    session = _fresh_session(settings)
    preferences_store.save(session, Preferences(run_history_keep=500))
    session.add(Setting(key="pref.removed_in_a_later_version", value="whatever"))
    session.commit()

    assert preferences_store.load(session).run_history_keep == 500


def test_redacted_hides_the_webhook_url() -> None:
    preferences = Preferences(notify_webhook_url=WEBHOOK)
    assert preferences.redacted()["notify_webhook_url"] == "***"
