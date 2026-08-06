"""Deleting connections and jobs from the web UI.

The JSON API has had delete since M1, but nothing in the UI called it, so a
mistyped connection was permanent unless you knew the API existed. Both guards
matter more than the delete does: a connection a job still points at, and a job
with a run in flight.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
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


def _session(settings: Settings) -> Session:
    return sessionmaker(bind=create_db_engine(settings))()


def _connections(settings: Settings) -> tuple[int, int]:
    session = _session(settings)
    source = Connection(name="src", type=ConnectionType.local, base_path="/data/source")
    dest = Connection(name="dst", type=ConnectionType.local, base_path="/data/dest")
    session.add_all([source, dest])
    session.commit()
    return source.id, dest.id


def _job(settings: Settings, source_id: int, dest_id: int, name: str = "Nightly") -> int:
    session = _session(settings)
    job = Job(
        name=name,
        source_connection_id=source_id,
        dest_connection_id=dest_id,
        max_delete_pct=20,
        filters={},
    )
    session.add(job)
    session.commit()
    return job.id


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


def test_an_unused_connection_can_be_deleted(authed_client: TestClient, settings: Settings) -> None:
    source_id, _dest_id = _connections(settings)

    response = authed_client.post(f"/connections/{source_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/connections"
    assert _session(settings).get(Connection, source_id) is None


def test_a_connection_a_job_uses_is_refused_by_name(
    authed_client: TestClient, settings: Settings
) -> None:
    """The database refuses this too, through RESTRICT. Catching it here turns an
    integrity error into a sentence naming the job to fix."""
    source_id, dest_id = _connections(settings)
    _job(settings, source_id, dest_id, name="Nightly Media")

    response = authed_client.post(f"/connections/{source_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "error=in-use" in response.headers["location"]
    assert "Nightly%20Media" in response.headers["location"]
    assert _session(settings).get(Connection, source_id) is not None

    page = authed_client.get(response.headers["location"]).text
    assert "still used by" in page
    assert "Nightly Media" in page


def test_the_delete_button_is_on_the_list(authed_client: TestClient, settings: Settings) -> None:
    _connections(settings)
    page = authed_client.get("/connections").text
    assert "/delete" in page
    assert ">Delete</button>" in page


def test_deleting_a_connection_that_is_already_gone_is_not_an_error(
    authed_client: TestClient,
) -> None:
    response = authed_client.post("/connections/9999/delete", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/connections"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def test_a_job_can_be_deleted(authed_client: TestClient, settings: Settings) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _job(settings, source_id, dest_id)

    response = authed_client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs"
    session = _session(settings)
    assert session.get(Job, job_id) is None
    # The connections it referenced are untouched.
    assert session.get(Connection, source_id) is not None


def test_a_job_with_a_run_in_flight_is_refused(
    authed_client: TestClient, settings: Settings
) -> None:
    """Deleting the row underneath a running rclone loses the record of work that
    is happening on disk right now."""
    source_id, dest_id = _connections(settings)
    job_id = _job(settings, source_id, dest_id)
    session = _session(settings)
    session.add(
        JobRun(
            job_id=job_id,
            trigger=RunTrigger.manual,
            mode=RunMode.live,
            status=RunStatus.running,
        )
    )
    session.commit()

    response = authed_client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}?error=running"
    assert _session(settings).get(Job, job_id) is not None

    page = authed_client.get(f"/jobs/{job_id}?error=running").text
    assert "run in progress" in page


def test_a_finished_run_does_not_block_deletion(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _job(settings, source_id, dest_id)
    session = _session(settings)
    session.add(
        JobRun(
            job_id=job_id,
            trigger=RunTrigger.manual,
            mode=RunMode.live,
            status=RunStatus.success,
        )
    )
    session.commit()

    response = authed_client.post(f"/jobs/{job_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert _session(settings).get(Job, job_id) is None
    # The run history went with it rather than being orphaned.
    assert _session(settings).query(JobRun).filter_by(job_id=job_id).count() == 0


def test_deleting_a_job_frees_its_connections(
    authed_client: TestClient, settings: Settings
) -> None:
    """The sequence someone actually needs: a connection cannot go until the job
    using it does."""
    source_id, dest_id = _connections(settings)
    job_id = _job(settings, source_id, dest_id)

    blocked = authed_client.post(f"/connections/{source_id}/delete", follow_redirects=False)
    assert "error=in-use" in blocked.headers["location"]

    authed_client.post(f"/jobs/{job_id}/delete", follow_redirects=False)
    freed = authed_client.post(f"/connections/{source_id}/delete", follow_redirects=False)

    assert freed.headers["location"] == "/connections"
    assert _session(settings).get(Connection, source_id) is None


def test_delete_needs_a_csrf_token(authed_client: TestClient, settings: Settings) -> None:
    """It is a destructive POST like any other."""
    source_id, _dest_id = _connections(settings)
    saved = authed_client.headers.pop("X-CSRF-Token")
    try:
        response = authed_client.post(f"/connections/{source_id}/delete")
    finally:
        authed_client.headers["X-CSRF-Token"] = saved

    assert response.status_code == 403
    assert _session(settings).get(Connection, source_id) is not None
