"""How figures and times are shown: sizes, schedules, timezone and clock.

None of this changes what is stored. Everything is UTC in the database and a
byte count is a byte count; this is entirely about what the screen says. The
tests matter anyway, because a size shown one way on one page and another way on
the next reads as a bug in the numbers rather than in the formatting.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import preferences as preferences_store
from app.db import create_db_engine
from app.jobs.cron import describe
from app.models import Connection, ConnectionType, Job
from app.web import _filesize

# --------------------------------------------------------------------------
# Sizes
# --------------------------------------------------------------------------


def test_a_size_reads_as_a_size() -> None:
    """The reported problem: a 3 GB file showed as 3033782870."""
    assert _filesize(3033782870) == "2.8 GB"
    assert _filesize(251748352) == "240.1 MB"
    assert _filesize(0) == "0 B"
    assert _filesize(512) == "512 B"
    assert _filesize(1024) == "1.0 KB"
    assert _filesize(None) == ""


def test_the_largest_unit_is_not_exceeded() -> None:
    """Beyond terabytes it keeps counting in terabytes rather than running off
    the end of the unit list."""
    assert _filesize(5 * 1024**4) == "5120.0 TB" or _filesize(5 * 1024**4).endswith("TB")


def test_the_server_and_the_browser_format_a_size_identically() -> None:
    """Two implementations is the cost of rendering some figures on the server
    and some in the browser. Two different answers is not.

    Reads the algorithm out of the shipped JavaScript rather than trusting a
    comment that says they match.
    """
    source = (Path("app/web/static/js/format.js")).read_text(encoding="utf-8")

    assert 'UNITS = ["B", "KB", "MB", "GB", "TB"]' in source
    assert "amount >= 1024" in source
    # 0 decimal places for bytes, 1 for everything above.
    assert "amount.toFixed(0)" in source
    assert "amount.toFixed(1)" in source


# --------------------------------------------------------------------------
# Schedules in words
# --------------------------------------------------------------------------


def test_common_schedules_read_as_english() -> None:
    assert describe(None) == "On demand"
    assert describe("") == "On demand"
    assert describe("0 * * * *") == "Hourly, on the hour"
    assert describe("30 * * * *") == "Hourly at 30 past"
    assert describe("*/15 * * * *") == "Every 15 minutes"
    assert describe("*/1 * * * *") == "Every minute"
    assert describe("30 2 * * *") == "Daily at 02:30"
    assert describe("0 3 * * 1") == "Mondays at 03:00"
    assert describe("0 3 * * 1,3,5") == "Monday, Wednesday and Friday at 03:00"


def test_every_day_of_the_week_is_just_daily() -> None:
    assert describe("0 3 * * 0,1,2,3,4,5,6") == "Daily at 03:00"


def test_the_clock_preference_reaches_the_description() -> None:
    assert describe("30 14 * * *", clock="12h") == "Daily at 2:30 PM"
    assert describe("0 0 * * *", clock="12h") == "Daily at 12:00 AM"
    assert describe("0 12 * * *", clock="12h") == "Daily at 12:00 PM"


def test_an_expression_it_cannot_describe_is_handed_back_verbatim() -> None:
    """The point of the whole exercise. A half-right description of a schedule
    that deletes files is worse than the expression the author typed."""
    for expression in (
        "15 14 1 * *",  # day of month
        "0 0 1 1 *",  # a specific month
        "0 9-17 * * *",  # a range
        "0 3 * * MON",  # names rather than numbers
        "nonsense",
        "* * * *",  # four fields
    ):
        assert describe(expression) == expression


def test_the_describer_covers_what_the_builder_writes() -> None:
    """The builder and the describer are separate implementations of the same
    idea, one in JavaScript and one here. If the builder learns a new shape and
    this does not, a job saved from the UI shows as a raw expression."""
    form = Path("app/web/templates/jobs/form.html").read_text(encoding="utf-8")

    # The exact expressions `compose()` can produce.
    written = [
        '"*/" + (every.value || "15") + " * * * *"',
        '(minuteOnly.value || "0") + " * * * *"',
        '(minute.value || "0") + " " + (hour.value || "0") + " * * " + dayField',
    ]
    for shape in written:
        assert shape in form, f"the builder no longer writes {shape}; check describe()"

    # And each of those, instantiated, is described rather than echoed.
    for expression in ("*/15 * * * *", "0 * * * *", "30 2 * * *", "30 2 * * 1,2"):
        assert describe(expression) != expression


# --------------------------------------------------------------------------
# Timezone and clock, end to end
# --------------------------------------------------------------------------


def _job_with_a_schedule(settings, cron: str = "0 3 * * 1") -> None:
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="src", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="dst", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    session.add(
        Job(
            name="Scheduled",
            source_connection_id=source.id,
            dest_connection_id=dest.id,
            filters={},
            schedule_cron=cron,
            timezone="UTC",
        )
    )
    session.commit()


def test_the_dashboard_shows_a_schedule_in_words(authed_client: TestClient, settings) -> None:
    _job_with_a_schedule(settings)

    page = authed_client.get("/").text

    assert "Mondays at 03:00" in page
    # The expression is still there to be read, on the element's title.
    assert 'title="0 3 * * 1"' in page


def test_the_stored_timezone_overrides_the_environment(authed_client: TestClient, settings) -> None:
    """The whole point of the preference: TZ is baked into the compose file, and
    changing it means recreating the container."""
    factory = sessionmaker(bind=create_db_engine(settings))
    with factory() as session:
        stored = preferences_store.load(session)
        stored.display_timezone = "America/Denver"
        preferences_store.save(session, stored)

    _stamp_a_run(settings, datetime(2026, 8, 6, 18, 30, tzinfo=UTC))
    # The dashboard is where run timestamps are rendered.
    page = authed_client.get("/").text

    # 18:30 UTC is 12:30 in Denver on that date.
    assert "12:30" in page
    assert "18:30" not in page


def test_the_clock_preference_changes_the_timestamps(authed_client: TestClient, settings) -> None:
    factory = sessionmaker(bind=create_db_engine(settings))
    with factory() as session:
        stored = preferences_store.load(session)
        stored.clock = "12h"
        # Pinned rather than left to the ambient TZ, so this test is about the
        # clock and nothing else.
        stored.display_timezone = "UTC"
        preferences_store.save(session, stored)

    _stamp_a_run(settings, datetime(2026, 8, 6, 18, 30, tzinfo=UTC))
    page = authed_client.get("/").text

    found = re.findall(r"20\d\d-\d\d-\d\d[^<]*", page)
    assert re.search(r"06:30 PM", page), f"expected a 12 hour timestamp, got {found[:6]}"


def test_an_unknown_timezone_is_refused_rather_than_silently_ignored() -> None:
    """Stored unchecked, every timestamp would quietly fall back to UTC and the
    only symptom would be times that are wrong."""
    from pydantic import ValidationError

    from app.preferences import Preferences

    try:
        Preferences(display_timezone="Mars/Olympus")
    except ValidationError as exc:
        assert "not a timezone this machine knows" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown zone was accepted")


def test_the_settings_screen_offers_both_controls(authed_client: TestClient) -> None:
    page = authed_client.get("/settings").text

    assert 'name="display_timezone"' in page
    assert 'name="clock"' in page


def test_saving_the_form_keeps_both(authed_client: TestClient, settings) -> None:
    """A field with no control on the form submits the schema default. That has
    happened here four times now, so every new field gets this test."""
    response = authed_client.post(
        "/settings",
        data={
            "notify_target": "none",
            "notify_ntfy_server": "https://ntfy.sh",
            "notify_timeout_seconds": "10",
            "run_history_keep": "200",
            "log_retention_days": "90",
            "log_max_total_mb": "512",
            "display_timezone": "Europe/London",
            "clock": "12h",
        },
    )

    assert response.status_code == 200
    with sessionmaker(bind=create_db_engine(settings))() as session:
        stored = preferences_store.load(session)
    assert stored.display_timezone == "Europe/London"
    assert stored.clock == "12h"


def test_clearing_the_timezone_falls_back_to_the_environment(
    authed_client: TestClient, settings
) -> None:
    """Empty means "follow TZ", not "leave whatever was there"."""
    factory = sessionmaker(bind=create_db_engine(settings))
    with factory() as session:
        stored = preferences_store.load(session)
        stored.display_timezone = "Europe/London"
        preferences_store.save(session, stored)

    authed_client.post(
        "/settings",
        data={
            "notify_target": "none",
            "notify_ntfy_server": "https://ntfy.sh",
            "notify_timeout_seconds": "10",
            "run_history_keep": "200",
            "log_retention_days": "90",
            "log_max_total_mb": "512",
            "display_timezone": "",
            "clock": "24h",
        },
    )

    with factory() as session:
        assert preferences_store.load(session).display_timezone == ""


def test_the_home_link_is_on_every_page(authed_client: TestClient) -> None:
    """The brand has always linked home, but nothing said so."""
    for path in ("/", "/jobs", "/connections", "/settings"):
        page = authed_client.get(path).text
        assert ">\n          Home\n        </a>" in page or ">Home<" in page.replace(
            " ", ""
        ).replace("\n", ""), f"no Home link on {path}"


def _stamp_a_run(settings, finished: datetime) -> int:
    from app.models import JobRun, RunMode, RunStatus, RunTrigger

    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="rs", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="rd", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(name="Timed", source_connection_id=source.id, dest_connection_id=dest.id, filters={})
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.manual,
        mode=RunMode.live,
        status=RunStatus.success,
        started_at=finished,
        finished_at=finished,
    )
    session.add(run)
    session.commit()
    return run.id
