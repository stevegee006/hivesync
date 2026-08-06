"""The README's Resilio migration walkthrough, executed in order.

M7's acceptance criterion is that the walkthrough works as written, so this
follows its numbered steps against the SMB fixture, through the same web forms a
reader would use rather than through the internals underneath them.

A documentation test is worth having here because the walkthrough is the only
part of this project that tells someone to point a deletion-capable tool at their
own NAS. If step 5 has drifted from the software, that is where it hurts.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import create_db_engine
from app.main import create_app
from app.models import DeleteMode, Job, JobRun, RunStatus
from tests.conftest import (
    NEW_PASSWORD,
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USERNAME,
    create_schema,
    make_settings,
    refresh_csrf,
)

pytestmark = pytest.mark.integration

SMB_HOST = os.environ.get("HIVESYNC_TEST_SMB_HOST", "smb")
SMB_PORT = int(os.environ.get("HIVESYNC_TEST_SMB_PORT", "445"))
# Unique per run: the fixture share is a persistent volume, so a leftover
# destination from an earlier run makes "2 files are new" false and the failure
# looks like a bug in the walkthrough rather than in the fixture.
SHARE_SUBDIR = f"readme-walkthrough-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = make_settings(tmp_path / "config")
    create_schema(settings)
    return settings


@pytest.fixture
def ui(settings: Settings):
    """A signed-in browser session, past the forced password change.

    Carries a CSRF token the way a browser does, by reading it off a rendered
    page, and picks up the new one after login, where it is rotated.
    """
    with TestClient(create_app(settings)) as client:
        refresh_csrf(client)
        client.post(
            "/api/auth/login",
            data={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
            follow_redirects=False,
        )
        refresh_csrf(client)
        client.post(
            "/api/auth/change-password",
            data={"current_password": TEST_ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
            follow_redirects=False,
        )
        yield client


def _db(settings: Settings) -> Session:
    return sessionmaker(bind=create_db_engine(settings))()


def _job_form(source_id: int, dest_id: int, **overrides: str) -> dict[str, str]:
    fields = {
        "name": "Photos to NAS",
        "source_connection_id": str(source_id),
        "dest_connection_id": str(dest_id),
        "source_path": "",
        "dest_path": SHARE_SUBDIR,
        "direction": "source_to_dest",
        "compare_mode": "mtime_size",
        "modify_window": "1s",
        "delete_mode": "none",
        "max_delete_pct": "20",
        "timezone": "UTC",
        "notify_on": "failure",
        "enabled": "true",
    }
    fields.update(overrides)
    return fields


def _run_to_completion(settings: Settings, client: TestClient, job_id: int, live: bool) -> JobRun:
    """Press the button the README says to press, then wait for the run."""
    import time

    endpoint = f"/jobs/{job_id}/run-live" if live else f"/jobs/{job_id}/run"
    response = client.post(endpoint, follow_redirects=False)
    assert response.status_code == 303, response.text
    run_id = int(response.headers["location"].rsplit("/", 1)[1])

    session = _db(settings)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        session.expire_all()
        run = session.get(JobRun, run_id)
        assert run is not None
        if run.status not in (RunStatus.queued, RunStatus.running):
            return run
        time.sleep(0.25)
    raise AssertionError("the run never finished")


def test_the_walkthrough_works_as_written(
    settings: Settings, ui: TestClient, tmp_path: Path
) -> None:
    # Step 3: create the connections, and test each one before moving on.
    staging = tmp_path / "staging"
    (staging / "album").mkdir(parents=True)
    (staging / "album" / "one.jpg").write_bytes(b"photo one\n")
    (staging / "album" / "two.jpg").write_bytes(b"photo two\n")

    created = ui.post(
        "/connections",
        data={
            "name": "staging",
            "type": "local",
            "base_path": str(staging),
            "extra_opts": "",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text

    credential = ui.post(
        "/credentials",
        data={"name": "nas-login", "kind": "password", "secret": "testpass"},
        follow_redirects=False,
    )
    assert credential.status_code == 303, credential.text

    session = _db(settings)
    from app.models import Credential

    credential_id = session.query(Credential).filter_by(name="nas-login").one().id

    created = ui.post(
        "/connections",
        data={
            "name": "nas",
            "type": "smb",
            "host": SMB_HOST,
            "port": str(SMB_PORT),
            "share": "testshare",
            "base_path": "",
            "username": "testuser",
            "credential_id": str(credential_id),
            "extra_opts": "",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text

    from app.models import Connection

    session.expire_all()
    source = session.query(Connection).filter_by(name="staging").one()
    dest = session.query(Connection).filter_by(name="nas").one()

    # "Test each one before moving on."
    for connection in (source, dest):
        result = ui.post(f"/connections/{connection.id}/test-partial")
        assert result.status_code == 200
        session.expire_all()
        assert session.get(Connection, connection.id).last_test_ok is True

    # Step 4: create the job with deletion handling left alone, and dry run it.
    response = ui.post("/jobs", data=_job_form(source.id, dest.id), follow_redirects=False)
    assert response.status_code == 303, response.text
    job_id = int(response.headers["location"].rsplit("/", 1)[1])

    session.expire_all()
    assert session.get(Job, job_id).delete_mode == DeleteMode.none

    dry = _run_to_completion(settings, ui, job_id, live=False)
    assert dry.status == RunStatus.success, dry.summary
    # "It lists every file that would be created ... and it changes nothing."
    assert dry.summary["new"] >= 2
    page = ui.get(f"/runs/{dry.id}").text
    assert "one.jpg" in page

    # Step 5's precondition: the live run applies exactly what the dry run said.
    live = _run_to_completion(settings, ui, job_id, live=True)
    assert live.status == RunStatus.success, live.summary
    assert live.files_transferred >= 2

    # Step 5: switch to archiving. The editor shows the resolved path, and the
    # brake is 20% unless changed.
    session.expire_all()
    job = session.get(Job, job_id)
    assert job.max_delete_pct == 20, "the README says the brake defaults to 20%"

    response = ui.post(
        f"/jobs/{job_id}",
        data=_job_form(source.id, dest.id, delete_mode="archive"),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    editor = ui.get(f"/jobs/{job_id}").text
    assert "Deletions go to" in editor, "the editor must show the resolved archive path"
    assert f"{SHARE_SUBDIR}.hivesync-archive" in editor

    # And archiving actually archives: delete on the source, sync, and the file
    # is gone from the destination rather than gone entirely.
    (staging / "album" / "two.jpg").unlink()
    archived = _run_to_completion(settings, ui, job_id, live=True)
    assert archived.status == RunStatus.success, archived.summary
    assert archived.files_archived == 1, archived.summary
    assert archived.files_deleted == 1

    # Step 6's advice: excluding Resilio's own metadata keeps it from replicating.
    (staging / ".sync").mkdir()
    (staging / ".sync" / "IgnoreList").write_text("resilio state\n", encoding="utf-8")

    response = ui.post(
        f"/jobs/{job_id}",
        data=_job_form(source.id, dest.id, delete_mode="archive", filters_exclude=".sync/**"),
        follow_redirects=False,
    )
    assert response.status_code == 303

    excluded = _run_to_completion(settings, ui, job_id, live=True)
    assert excluded.status == RunStatus.success, excluded.summary
    assert excluded.files_transferred == 0, (
        "the exclude did not hold: Resilio's metadata was replicated"
    )


def test_the_dsm_preset_the_readme_names_exists(settings: Settings, ui: TestClient) -> None:
    """The Synology section tells people to apply a preset by name. If it is
    renamed, that instruction becomes unfollowable."""
    page = ui.get("/filter-presets").text
    assert "Synology / DSM" in page
    assert "@eaDir" in page
    assert "#recycle" in page


def test_the_dsm_preset_excludes_metadata_at_the_top_of_the_tree(tmp_path: Path) -> None:
    """The pattern form matters, and it is not the obvious one.

    `**/@eaDir/**` reads as "@eaDir at any depth" and is not: rclone requires at
    least one directory in front of the name, so it misses the `@eaDir` at the
    top of the synced folder. DSM puts one there. This ran against the real
    binary with the preset that shipped and found it, so it stays.
    """
    import subprocess

    from app.filter_presets import BUILTIN_PRESETS

    tree = tmp_path / "share"
    for relative in ("@eaDir", "album/@eaDir", "album/deep/@eaDir", "#recycle", "album/@tmp"):
        (tree / relative).mkdir(parents=True)
        (tree / relative / "junk").write_bytes(b"x")
    (tree / "keep.txt").write_bytes(b"x")
    (tree / ".DS_Store").write_bytes(b"x")
    (tree / "album" / "photo.jpg").write_bytes(b"x")

    argv = ["rclone", "lsf", "-R", str(tree), "--files-only", "--color", "NEVER"]
    for pattern in BUILTIN_PRESETS["Synology / DSM"]:
        argv += ["--exclude", pattern]
    listed = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603

    assert sorted(listed.split()) == ["album/photo.jpg", "keep.txt"], listed
