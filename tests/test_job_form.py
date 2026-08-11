"""The job editor, which is where a mis-saved field becomes a deleted file.

A form that silently drops a field is worse than one that rejects it: the job
keeps running, with a policy nobody chose. Both cases below were real.
"""

from __future__ import annotations

from pathlib import Path

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


# --------------------------------------------------------------------------
# The schedule builder
# --------------------------------------------------------------------------


def test_the_builder_is_offered_alongside_the_raw_expression(
    authed_client: TestClient, settings: Settings
) -> None:
    """A builder cannot express everything cron can, so the field stays."""
    source_id, dest_id = _connections(settings)
    job_id = _create(authed_client, _form(source_id, dest_id))

    page = authed_client.get(f"/jobs/{job_id}").text

    assert 'id="schedule-builder"' in page
    assert 'data-cron="mode"' in page
    assert 'name="schedule_cron"' in page


def test_a_schedule_typed_by_hand_still_saves(
    authed_client: TestClient, settings: Settings
) -> None:
    """The builder writes the field; it must not become the only way in."""
    source_id, dest_id = _connections(settings)
    job_id = _create(authed_client, _form(source_id, dest_id, schedule_cron="15 2,14 * * 1-5"))

    job = _saved(settings, job_id)
    assert job.schedule_cron == "15 2,14 * * 1-5"

    # And it comes back untouched rather than being rewritten into something the
    # builder can represent.
    assert "15 2,14 * * 1-5" in authed_client.get(f"/jobs/{job_id}").text


def test_a_job_can_be_continuous_instead_of_scheduled(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)
    job_id = _create(authed_client, _form(source_id, dest_id, continuous="true", schedule_cron=""))

    job = _saved(settings, job_id)
    assert job.continuous is True
    assert not job.schedule_cron


def test_continuous_plus_a_schedule_is_refused_by_the_form(
    authed_client: TestClient, settings: Settings
) -> None:
    source_id, dest_id = _connections(settings)

    response = authed_client.post(
        "/jobs",
        data=_form(source_id, dest_id, continuous="true", schedule_cron="0 2 * * *"),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "either continuous or scheduled" in response.text


# --------------------------------------------------------------------------
# The quiet period, which now applies to every run
# --------------------------------------------------------------------------


def test_the_quiet_period_is_not_inside_the_continuous_block() -> None:
    """It was continuous only, so a scheduled job had no protection against a
    download client writing into the source. The control has to be reachable
    without turning watching on."""
    form = Path("app/web/templates/jobs/form.html").read_text(encoding="utf-8")

    field = form.index('name="quiet_period_seconds"')
    continuous = form.index("continuous: {{")
    assert field < continuous, (
        "the quiet period control is inside the continuous section, so it cannot "
        "be set on a scheduled job"
    )


def test_a_scheduled_job_passes_the_quiet_period() -> None:
    """The whole point of the change. A job that is not continuous still needs a
    file that is being written left alone."""
    from app.engines.rclone import quiet_period_args
    from app.models import Job

    scheduled = Job(name="s", continuous=False, quiet_period_seconds=30)
    assert quiet_period_args(scheduled) == ["--min-age", "30s"]


def test_zero_turns_the_quiet_period_off() -> None:
    from app.engines.rclone import quiet_period_args
    from app.models import Job

    assert quiet_period_args(Job(name="s", continuous=False, quiet_period_seconds=0)) == []
    assert quiet_period_args(Job(name="c", continuous=True, quiet_period_seconds=0)) == []


def test_the_plan_and_the_run_agree_about_which_files_they_see() -> None:
    """Without this a dry run lists a file the live run then skips, and the two
    disagree about the same tree."""
    source = Path("app/engines/rclone.py").read_text(encoding="utf-8")

    shared = source[source.index("shared = filter_args(job)") :]
    assert "quiet_period_args(job)" in shared.split("\n")[0], (
        "the planning phases do not pass --min-age, so a preview sees files the run will skip"
    )


def test_bisync_passes_it_too() -> None:
    """It writes to both sides, so a file still being written is two chances to
    copy it half finished."""
    source = Path("app/engines/bisync.py").read_text(encoding="utf-8")

    assert "quiet_period_args(job)" in source


def test_a_preset_excludes_the_names_download_clients_write() -> None:
    """Modification time catches a file still growing. A client that writes
    under a temporary name and renames at the end needs the name excluded."""
    from app.filter_presets import BUILTIN_PRESETS

    rules = BUILTIN_PRESETS["Downloads in progress"]

    for pattern in ("*.part", "*.!qB", "*.crdownload", "*.partial"):
        assert pattern in rules, f"{pattern} is missing"
    # Unanchored, so they match at every level rather than only the sync root.
    assert not any(rule.startswith("**/") for rule in rules)


def test_the_quiet_period_default_is_stated_once() -> None:
    """It lived in four places: the column, the schema, the form handler and the
    template. The handler's copy was still 30 after the others became 0, so a
    job created without the field got a quiet period nobody chose, and once this
    applied to every run that job silently copied nothing it had just been given.
    """
    from app.models import Job
    from app.schemas.job import JobCreate

    schema_default = JobCreate.model_fields["quiet_period_seconds"].default
    column_default = Job.__table__.c.quiet_period_seconds.default.arg
    assert schema_default == column_default == 0

    # The handler derives it rather than repeating it.
    web = Path("app/web/__init__.py").read_text(encoding="utf-8")
    assert 'JobCreate.model_fields["quiet_period_seconds"].default' in web

    # And the template offers the same number for a new job.
    form = Path("app/web/templates/jobs/form.html").read_text(encoding="utf-8")
    assert "job.quiet_period_seconds if job else 0" in form
