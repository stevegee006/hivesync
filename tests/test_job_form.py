"""The job editor, which is where a mis-saved field becomes a deleted file.

A form that silently drops a field is worse than one that rejects it: the job
keeps running, with a policy nobody chose. Both cases below were real.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_db_engine
from app.models import (
    ArchiveLayout,
    ConflictResolve,
    Connection,
    ConnectionType,
    DeleteMode,
    Job,
    NotifyOn,
)

# Deliberately not tmp_path: these are display paths, never opened, and a
# Windows tmp_path would put backslashes into a form the container renders with
# forward slashes.
SOURCE_BASE = "/mnt/tank/incoming"
DEST_BASE = "/mnt/tank/media"


def _connections(settings: Settings) -> tuple[int, int]:
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="src", type=ConnectionType.local, base_path=SOURCE_BASE)
    dest = Connection(name="dst", type=ConnectionType.local, base_path=DEST_BASE)
    session.add_all([source, dest])
    session.commit()
    ids = (source.id, dest.id)
    session.close()
    return ids


def _form(source_id: int, dest_id: int, **overrides: str) -> dict[str, str]:
    fields = {
        "name": "Nightly",
        "source_connection_id": str(source_id),
        "dest_connection_id": str(dest_id),
        "source_path": "",
        "dest_path": "",
        "direction": "source_to_dest",
        "compare_mode": "mtime_size",
        "modify_window": "1s",
        "delete_mode": "none",
        "max_delete_pct": "20",
        "timezone": "UTC",
        "enabled": "true",
    }
    fields.update(overrides)
    return fields


def _saved(settings: Settings, job_id: int) -> Job:
    session = sessionmaker(bind=create_db_engine(settings))()
    job = session.get(Job, job_id)
    assert job is not None
    return job


def _create(client: TestClient, form: dict[str, str]) -> int:
    response = client.post("/jobs", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


# --------------------------------------------------------------------------
# Archiving
# --------------------------------------------------------------------------


def test_archiving_can_be_chosen_and_its_fields_persist(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _create(
        authed_client,
        _form(
            source_id,
            dest_id,
            delete_mode="archive",
            archive_base="/mnt/tank/attic",
            archive_layout="suffix",
        ),
    )

    job = _saved(settings, job_id)
    assert job.delete_mode == DeleteMode.archive
    assert job.archive_base == "/mnt/tank/attic"
    assert job.archive_layout == ArchiveLayout.suffix


def test_switching_away_from_archiving_clears_the_path(
    authed_client: TestClient, settings: Settings
) -> None:
    """The archive inputs are hidden rather than removed, so they keep submitting
    a stale value. Left alone, saving the job would be refused for setting a path
    it is no longer using."""
    source_id, dest_id = _connections(settings)
    job_id = _create(
        authed_client,
        _form(source_id, dest_id, delete_mode="archive", archive_base="/mnt/tank/attic"),
    )

    response = authed_client.post(
        f"/jobs/{job_id}",
        data=_form(source_id, dest_id, delete_mode="delete", archive_base="/mnt/tank/attic"),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    job = _saved(settings, job_id)
    assert job.delete_mode == DeleteMode.delete
    assert job.archive_base is None


def test_the_resolved_archive_path_and_its_exclude_are_shown(
    authed_client: TestClient, settings: Settings
) -> None:
    """ "Beside the destination" is not something an operator can check against
    their own filesystem, and neither is a filter this adds on their behalf."""
    source_id, dest_id = _connections(settings)
    job_id = _create(
        authed_client,
        _form(source_id, dest_id, delete_mode="archive", archive_base=f"{DEST_BASE}/.attic"),
    )

    page = authed_client.get(f"/jobs/{job_id}").text
    assert f"{DEST_BASE}/.attic/nightly/" in page
    # The injected filter, named, because the operator did not write it.
    assert "/.attic/**" in page


def test_an_impossible_archive_path_is_explained_on_the_form(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _create(
        authed_client, _form(source_id, dest_id, delete_mode="archive", archive_base=DEST_BASE)
    )

    page = authed_client.get(f"/jobs/{job_id}").text
    assert "same as the destination" in page


# --------------------------------------------------------------------------
# Fields the form used to drop
# --------------------------------------------------------------------------


def test_editing_a_bidirectional_job_keeps_its_conflict_policy(
    authed_client: TestClient, settings: Settings
) -> None:
    """The form carried no conflict control, so every save through the web UI
    reset the policy to the default. A job set to prefer one side would quietly
    start preferring the newer file instead."""
    source_id, dest_id = _connections(settings)
    job_id = _create(
        authed_client,
        _form(
            source_id,
            dest_id,
            direction="bidirectional",
            delete_mode="none",
            conflict_resolve="path1",
            check_access="true",
        ),
    )
    assert _saved(settings, job_id).conflict_resolve == ConflictResolve.path1

    # An edit that touches something else entirely.
    response = authed_client.post(
        f"/jobs/{job_id}",
        data=_form(
            source_id,
            dest_id,
            name="Renamed",
            direction="bidirectional",
            delete_mode="none",
            conflict_resolve="path1",
            check_access="true",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    job = _saved(settings, job_id)
    assert job.name == "Renamed"
    assert job.conflict_resolve == ConflictResolve.path1
    assert job.check_access is True


def test_an_unchecked_access_marker_stays_unchecked(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _create(
        authed_client,
        _form(source_id, dest_id, direction="bidirectional", delete_mode="none"),
    )
    assert _saved(settings, job_id).check_access is False


def test_editing_a_job_keeps_its_notification_setting(
    authed_client: TestClient, settings: Settings
) -> None:
    """Same failure mode as the conflict policy: a field with no control on the
    form submits the schema default on every save."""
    source_id, dest_id = _connections(settings)
    job_id = _create(authed_client, _form(source_id, dest_id, notify_on="always"))
    assert _saved(settings, job_id).notify_on == NotifyOn.always

    response = authed_client.post(
        f"/jobs/{job_id}",
        data=_form(source_id, dest_id, name="Renamed", notify_on="always"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _saved(settings, job_id).notify_on == NotifyOn.always


def test_the_form_offers_the_notification_control(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _create(authed_client, _form(source_id, dest_id, notify_on="never"))

    page = authed_client.get(f"/jobs/{job_id}").text

    assert 'name="notify_on"' in page
    assert 'value="never" selected' in page
