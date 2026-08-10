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


def test_every_navigation_icon_has_an_accessible_name(authed_client: TestClient) -> None:
    """The nav is glyphs now. Without a name each one is a guessing game for
    anyone using a screen reader and a memory test for everyone else."""
    page = authed_client.get("/").text

    for label, href in [
        ("Home", "/"),
        ("Jobs", "/jobs"),
        ("Connections", "/connections"),
        ("Credentials", "/credentials"),
        ("Filters", "/filter-presets"),
        ("Settings", "/settings"),
    ]:
        assert f'href="{href}"' in page, f"no link to {href}"
        assert f'aria-label="{label}"' in page, f"the {label} icon has no accessible name"
        assert f'title="{label}"' in page, f"the {label} icon has no tooltip"


def test_the_account_menu_works_without_javascript(authed_client: TestClient) -> None:
    """It holds sign out and password change. A scripted dropdown would put both
    behind an asset that base.html promises the page works without."""
    page = authed_client.get("/").text

    assert "<details" in page
    assert 'action="/api/auth/logout"' in page
    assert "/account/password" in page
    # Not Alpine: x-show would leave the menu unopenable if the script failed.
    # Anchored on data-account, since the header now has two disclosures.
    start = page.index("data-account")
    menu = page[start : page.index("</details>", start)]
    assert "x-show" not in menu and "x-data" not in menu


def test_the_disclosure_triangle_is_hidden() -> None:
    """Both rules are needed. Chrome and Safari use the pseudo-element, Firefox
    uses list-style, and half a fix leaves a stray triangle by the icon."""
    css = Path("app/web/static/css/tailwind.src.css").read_text(encoding="utf-8")

    assert "summary::-webkit-details-marker" in css
    assert "list-style: none" in css


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


# --------------------------------------------------------------------------
# Mobile layout
#
# Verified by rendering every authenticated page at 375px in a real browser.
# These pin the rules that verification established, because the next template
# added will not be looked at on a phone.
# --------------------------------------------------------------------------

TEMPLATE_DIR = Path("app/web/templates")


def test_the_page_declares_a_viewport() -> None:
    """Without it a phone renders at 980px and scales down, so every fix below
    is invisible and the whole UI is a zoomed-out desktop page."""
    base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")

    assert 'name="viewport"' in base
    assert "width=device-width" in base


def test_every_table_can_scroll_sideways() -> None:
    """A table of connections or changes cannot fit 375px and must not be what
    makes the whole page scroll horizontally. Each one lives in its own
    overflow-x-auto box instead."""
    for path in TEMPLATE_DIR.rglob("*.html"):
        body = path.read_text(encoding="utf-8")
        tables = body.count("<table")
        if not tables:
            continue
        wrappers = body.count("overflow-x-auto")
        assert wrappers >= tables, (
            f"{path.name} has {tables} table(s) and {wrappers} scroll wrapper(s). "
            "A table without one makes the entire page scroll sideways on a phone."
        )


def test_the_activity_strip_clears_the_content_at_every_width() -> None:
    """The strip is position-fixed, so this padding is the only thing keeping it
    off the bottom of the page. It was a single value while the strip was three
    times taller on a phone than on a desktop, so it covered the last job card
    and its buttons."""
    base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")

    main_tag = re.search(r"<main[^>]*>", base)
    assert main_tag, "no <main> in base.html"
    classes = main_tag.group(0)
    assert "pb-28" in classes and "sm:pb-40" in classes, (
        f"the clearance below the fixed strip is no longer responsive: {classes}"
    )


def test_the_strips_fixed_columns_are_hidden_when_they_do_not_fit() -> None:
    """Three 176px columns plus a chart need about 900px. Below that the chart
    was crushed to nothing and the range buttons collided with the run count."""
    base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")

    fixed_columns = re.findall(r'class="([^"]*\bw-44\b[^"]*)"', base)
    assert fixed_columns, "the strip no longer has fixed columns; check this test"
    for classes in fixed_columns:
        assert "hidden" in classes or "sm:w-44" in classes, (
            f"a 176px column with no small-screen handling: {classes}"
        )


def test_the_run_page_reads_keys_that_a_live_run_actually_writes() -> None:
    """The reported bug, and the shape of it.

    The summary cards read `new`, `updated` and `unchanged`. Only a dry run
    wrote those; a live run wrote `planned_new`, `transferred` and no
    `unchanged` at all. So every live run showed New 0, Updated 0 and Unchanged
    0 however many files it moved, and nothing failed, because a missing key in
    `summary.get(key, 0)` is indistinguishable from a genuine zero.
    """
    template = (TEMPLATE_DIR / "runs" / "detail.html").read_text(encoding="utf-8")
    runner = Path("app/jobs/runner.py").read_text(encoding="utf-8")

    read = set(re.findall(r"summary\.get\(\s*'([a-z_]+)'", template))
    assert {"new", "updated", "unchanged", "bytes"} <= read, read

    # The live summary literal, up to its closing brace.
    start = runner.index("run.summary = {\n        # The same keys a dry run writes")
    written = set(re.findall(r'"([a-z_]+)":', runner[start : runner.index("\n    }", start)]))

    missing = {key for key in read if key not in written}
    # `rows_omitted` is set by the page's own query rather than by a run.
    #
    # `bidirectional` is a flag whose absence is the correct answer: this is the
    # one way summary, and a missing boolean reads as False, which is true. The
    # bisync summary writes it, and `test_a_live_bisync_run_reports_its_own_brake`
    # covers that. The rule this test enforces is about *counts*, where a missing
    # key is indistinguishable from a real zero and silently renders as one.
    missing -= {"rows_omitted", "bidirectional"}
    assert not missing, (
        f"the run page reads {sorted(missing)} which no live run writes, so they "
        "will silently render as zero"
    )


def test_the_engine_versions_live_in_the_header(authed_client: TestClient) -> None:
    """They were a row of cards on the dashboard and a footer on every page,
    saying the same thing twice and taking a third of a phone screen."""
    page = authed_client.get("/").text

    assert "1.74.4" in page
    assert "/api/health" in page
    assert "data-about" in page
    # And not the two places they used to be.
    assert "<footer" not in page
    assert ">Engines</h2>" not in page


def test_the_app_has_a_favicon(authed_client: TestClient) -> None:
    """Without one the tab shows the browser's default globe, which is what
    every other unidentified tab shows."""
    page = authed_client.get("/").text
    assert 'rel="icon"' in page
    assert "/static/icon.svg" in page

    response = authed_client.get("/static/icon.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]


def test_the_favicon_is_readable_at_a_tab_size() -> None:
    """It is drawn at 16px almost always. The hole is punched with evenodd
    rather than painted in a background colour, because a tab strip can be light
    or dark and a faked hole would show as a blob on one of them."""
    icon = Path("app/web/static/icon.svg").read_text(encoding="utf-8")

    assert 'fill-rule="evenodd"' in icon
    # No strokes: under about 2px they turn to mush at tab size. Matching the
    # attribute, not the word, which appears in the file's own comment.
    assert "stroke=" not in icon


def test_the_login_page_carries_the_icon_too(client: TestClient) -> None:
    """It extends the same base, but this is the one page an unauthenticated
    visitor sees and it is worth knowing it did not lose the head."""
    page = client.get("/login").text

    assert "/static/icon.svg" in page


def test_the_header_logo_is_the_same_file_as_the_favicon(authed_client: TestClient) -> None:
    """One file, so the tab and the header cannot drift apart. Also `self`
    under the content security policy, which a data URI or a CDN would not be."""
    page = authed_client.get("/").text
    header = page[page.index("<header") : page.index("</header>")]

    assert '<img src="/static/icon.svg"' in header
    assert "HiveSync" in header


# --------------------------------------------------------------------------
# The job card
# --------------------------------------------------------------------------


def _card_job(settings, *, continuous: bool = False, running: bool = False) -> None:
    from datetime import UTC, datetime

    from app.models import JobRun, RunMode, RunStatus, RunTrigger

    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="cs", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="cd", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Carded",
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
        schedule_cron=None if continuous else "0 * * * *",
        continuous=continuous,
    )
    session.add(job)
    session.commit()
    if running:
        session.add(
            JobRun(
                job_id=job.id,
                trigger=RunTrigger.manual,
                mode=RunMode.dry_run,
                status=RunStatus.running,
                started_at=datetime(2026, 8, 6, 22, 36, tzinfo=UTC),
                finished_at=None,
            )
        )
        session.commit()


def test_a_running_job_does_not_say_it_finished_never(authed_client: TestClient, settings) -> None:
    """A run still going has no finish time, and the localtime filter renders a
    missing timestamp as "never", so the card read "running never"."""
    _card_job(settings, running=True)

    page = authed_client.get("/").text

    assert "running" in page
    assert "running\n            never" not in page.replace("\r\n", "\n")
    assert re.search(r"running\s*</a>\s*<span[^>]*>\s*since ", page), (
        "a running job should say when it started"
    )


def test_last_checked_is_only_shown_for_a_watching_job(authed_client: TestClient, settings) -> None:
    """It is the proof a watching job is still looking, and a watching job that
    finds nothing records no run. On a scheduled job it is meaningless, and a
    labelled empty cell reads as a missing figure rather than an inapplicable
    one."""
    _card_job(settings, continuous=False)

    assert "Last checked" not in authed_client.get("/").text


def test_a_watching_job_does_show_last_checked(authed_client: TestClient, settings) -> None:
    _card_job(settings, continuous=True)

    page = authed_client.get("/").text

    assert "Last checked" in page
    # And says what it means, since the name alone does not.
    assert "still looking" in page


def test_the_continuous_help_text_says_polling_plainly() -> None:
    """The previous wording ("no backend HiveSync supports announces one") was
    hard to parse, and this is the sentence that sets expectations for the whole
    feature."""
    form = (TEMPLATE_DIR / "jobs" / "form.html").read_text(encoding="utf-8")

    assert "polling, not watching" in form
    assert "announces one" not in form


def test_the_login_page_shows_the_logo(client: TestClient) -> None:
    page = client.get("/login").text

    assert '<img src="/static/icon.svg"' in page
    assert "HiveSync" in page


def test_the_scrollbar_gutter_is_reserved_on_every_page() -> None:
    """Without this the usable width changes by about 15px between a page that
    scrolls and one that does not. The content is centred in a max-width
    container, so everything jumped sideways on navigation. Settings was the
    obvious one because it is the tallest, but it happened on any pair where one
    page scrolls and the other does not.

    Measured in a browser: `main` ended at the same x on a scrolling and a
    non-scrolling page once this was set, and 15px apart before.
    """
    css = Path("app/web/static/css/tailwind.src.css").read_text(encoding="utf-8")

    assert "scrollbar-gutter: stable" in css
    # And a fallback, so a browser without it still gets a constant width.
    assert "overflow-y: scroll" in css


# --------------------------------------------------------------------------
# The dashboard keeps itself current
# --------------------------------------------------------------------------


def test_the_dashboard_refreshes_itself(authed_client: TestClient, settings) -> None:
    """It is a snapshot of state that changes with nobody touching the page: a
    scheduled run starts, a run finishes. Without this a finished job still sat
    under "Running now" until someone pressed reload."""
    _card_job(settings, running=True)

    page = authed_client.get("/").text

    assert 'id="dashboard-body"' in page
    assert 'hx-get="/"' in page
    assert 'hx-select="#dashboard-body"' in page


def test_the_poll_tightens_while_a_run_is_active(authed_client: TestClient, settings) -> None:
    """Polling the whole dashboard every few seconds forever is waste on an idle
    instance. The server picks the interval on each swap, so it adjusts itself."""
    _card_job(settings, running=True)
    busy = authed_client.get("/").text
    assert 'hx-trigger="every 5s"' in busy


def test_the_poll_relaxes_when_nothing_is_running(authed_client: TestClient, settings) -> None:
    _card_job(settings, running=False)
    idle = authed_client.get("/").text
    assert 'hx-trigger="every 30s"' in idle


def test_the_activity_strip_is_outside_the_swapped_region(authed_client: TestClient) -> None:
    """Swapping an element that contains a live pane is what made the run page
    look like it was refreshing on a loop. The strip must survive every swap, so
    it has to sit outside the element the poll replaces."""
    page = authed_client.get("/").text

    swapped = page.index('id="dashboard-body"')
    end_of_main = page.index("</main>")
    strip = page.index('id="activity-strip"')

    assert swapped < end_of_main, "the swapped element should be inside main"
    assert strip > end_of_main, "the activity strip must not be inside the swapped element"


def test_a_poll_that_lands_on_the_login_page_does_not_blank_the_dashboard() -> None:
    """When a session expires the poll follows the redirect to /login, where
    there is no #dashboard-body to select, and htmx would swap in nothing and
    leave an empty page. The guard reloads instead, so the reader gets the login
    form rather than a blank screen."""
    base = Path("app/web/templates/base.html").read_text(encoding="utf-8")

    assert "htmx:beforeSwap" in base
    assert "/login" in base


def test_a_live_bisync_run_reports_its_own_brake() -> None:
    """--max-delete is a percentage for bisync and a count for sync, so the run
    page needs to know which it is looking at. Without the flag a live
    bidirectional run showed the count based sentence, naming a threshold rclone
    does not enforce."""
    runner = Path("app/jobs/runner.py").read_text(encoding="utf-8")

    # Anchored on a neighbouring key that is not itself under test, so the slice
    # covers the whole bisync summary rather than starting after the key being
    # asserted on.
    marker = runner.index('"resync": run.is_resync')
    start = runner.rindex("run.summary = {", 0, marker)
    summary = runner[start : runner.index("\n    }", start)]

    assert '"bidirectional": True' in summary
    assert '"max_delete_threshold": job.max_delete_pct' in summary


# --------------------------------------------------------------------------
# The run history on the job page
# --------------------------------------------------------------------------


def _job_with_runs(settings) -> int:
    from app.models import JobRun, RunMode, RunStatus, RunTrigger

    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="hs", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="hd", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name="Historied", source_connection_id=source.id, dest_connection_id=dest.id, filters={}
    )
    session.add(job)
    session.commit()
    when = datetime(2026, 8, 10, 22, 38, tzinfo=UTC)
    for mode in (RunMode.live, RunMode.dry_run):
        session.add(
            JobRun(
                job_id=job.id,
                trigger=RunTrigger.manual,
                mode=mode,
                status=RunStatus.success,
                started_at=when,
                finished_at=when,
                files_transferred=1,
                files_deleted=0,
            )
        )
    session.commit()
    return job.id


def test_the_run_history_uses_the_configured_timezone(authed_client: TestClient, settings) -> None:
    """It was the one table calling strftime directly, so it showed raw UTC and
    disagreed with the same run on the dashboard by the whole offset."""
    factory = sessionmaker(bind=create_db_engine(settings))
    with factory() as session:
        stored = preferences_store.load(session)
        stored.display_timezone = "America/Denver"
        preferences_store.save(session, stored)

    job_id = _job_with_runs(settings)
    page = authed_client.get(f"/jobs/{job_id}").text

    # 22:38 UTC is 16:38 in Denver on that date.
    assert "16:38" in page
    assert "22:38" not in page


def test_a_finished_live_run_is_not_described_in_the_future_tense(
    authed_client: TestClient, settings
) -> None:
    """Every successful run said "1 to transfer, 0 to delete", so a sync that had
    already moved a file read like a plan it had not carried out yet."""
    job_id = _job_with_runs(settings)

    page = authed_client.get(f"/jobs/{job_id}").text

    assert "1 transferred, 0 deleted" in page
    assert "1 would transfer, 0 would delete" in page
    assert "to transfer" not in page


def test_a_bidirectional_live_run_reports_what_it_reconciled() -> None:
    """It showed New 0 and Updated 0 however much it moved, because the cards
    read `new`/`updated` and the bisync summary only had a per-side breakdown.
    The same defect as the one way summary, fixed in only one of the two."""
    runner = Path("app/jobs/runner.py").read_text(encoding="utf-8")

    marker = runner.index('"resync": run.is_resync')
    start = runner.rindex("run.summary = {", 0, marker)
    summary = runner[start : runner.index("\n    }", start)]

    # From what the run did, not from the per-side deltas: a resync emits no
    # delta lines, so deriving these from them reported zero for a first sync.
    assert '"new": len(observed.created)' in summary
    assert '"updated": len(observed.replacements)' in summary
    assert '"bytes": run.bytes_transferred' in summary
    # Not invented: bisync does not report files it left alone.
    assert '"unchanged"' not in summary


def test_unchanged_is_a_dash_rather_than_a_zero_for_a_bidirectional_run() -> None:
    """A measurement that was never taken must not render as one that was."""
    template = (TEMPLATE_DIR / "runs" / "detail.html").read_text(encoding="utf-8")

    assert "mdash" in template
    assert "summary.get('bidirectional')" in template


# --------------------------------------------------------------------------
# Template syntax that leaks onto the page
# --------------------------------------------------------------------------


def test_no_page_renders_template_syntax_as_content(authed_client: TestClient, settings) -> None:
    """Jinja comments do not nest. Writing the closing marker as literal text
    inside a comment ends it there, and the remainder is rendered to the page:
    a paragraph of commentary about the template appeared above the summary
    cards on every run page.

    Checked by rendering, not by reading the source, because the source looked
    like a comment and behaved like content.
    """
    run_id = _job_with_runs(settings)
    pages = ["/", "/jobs", f"/jobs/{run_id}", "/connections", "/settings", "/credentials"]

    for path in pages:
        body = authed_client.get(path).text
        assert "#}" not in body, f"{path} renders a stray comment terminator"
        assert "{#" not in body, f"{path} renders an unopened comment"
        assert "{%" not in body, f"{path} renders an unevaluated statement"


def test_every_template_still_compiles() -> None:
    """A syntax error in a template raises at render time rather than at import,
    so a broken one ships happily and 500s the page instead."""
    from app.web import TEMPLATE_DIR, templates

    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        name = path.relative_to(TEMPLATE_DIR).as_posix()
        templates.env.get_template(name)


def test_a_bidirectional_live_run_records_the_files_it_moved() -> None:
    """The counts were right and the table said "Nothing changed", because only
    the one way runner ever wrote per-file rows."""
    runner = Path("app/jobs/runner.py").read_text(encoding="utf-8")

    marker = runner.index('"resync": run.is_resync')
    start = runner.rindex("def _record_bisync", 0, marker)
    body = runner[start:marker]

    assert "JobRunChange(" in body
    assert "bisync.parse_planned_changes(text)" in body


def test_a_resync_counts_what_it_copied_rather_than_what_it_diffed() -> None:
    """A resync makes one side match the other with no diff phase, so it emits
    no per-side delta lines. Deriving the cards from those reported new 0 and
    updated 0 for a first sync that had plainly copied a file."""
    runner = Path("app/jobs/runner.py").read_text(encoding="utf-8")

    marker = runner.index('"resync": run.is_resync')
    start = runner.rindex("run.summary = {", 0, marker)
    summary = runner[start : runner.index("\n    }", start)]

    assert '"new": len(observed.created)' in summary
    assert '"updated": len(observed.replacements)' in summary


def test_the_change_table_says_which_way_each_file_flowed(
    authed_client: TestClient, settings
) -> None:
    """A bidirectional run can have two rows travelling opposite ways, so the
    action and path alone do not say where a file came from."""
    from app.models import (
        ChangeAction,
        ChangeSide,
        JobRun,
        JobRunChange,
        RunMode,
        RunStatus,
        RunTrigger,
    )

    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="UltraCC", type=ConnectionType.local, base_path="/s")
    dest = Connection(name="Synology", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(name="Flowed", source_connection_id=source.id, dest_connection_id=dest.id, filters={})
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.manual,
        mode=RunMode.dry_run,
        status=RunStatus.success,
        summary={"new": 1, "updated": 1},
    )
    session.add(run)
    session.commit()
    session.add_all(
        [
            JobRunChange(
                run_id=run.id, action=ChangeAction.new, side=ChangeSide.dest, path="down.txt"
            ),
            JobRunChange(
                run_id=run.id, action=ChangeAction.new, side=ChangeSide.source, path="up.txt"
            ),
        ]
    )
    session.commit()

    page = authed_client.get(f"/runs/{run.id}").text

    assert ">Flow</th>" in page
    # Both directions are named, using the connections rather than "source".
    assert "UltraCC" in page and "Synology" in page


def test_the_flow_label_carries_the_subpath(authed_client: TestClient, settings) -> None:
    """A bidirectional job commonly pairs two subpaths of the same connection.
    The names alone are then identical, and the column read "Synology Test ->
    Synology Test", which says nothing about which way anything went."""
    from app.models import (
        ChangeAction,
        ChangeSide,
        JobRun,
        JobRunChange,
        RunMode,
        RunStatus,
        RunTrigger,
    )

    session = sessionmaker(bind=create_db_engine(settings))()
    both = Connection(name="Synology", type=ConnectionType.local, base_path="/data")
    session.add(both)
    session.commit()
    job = Job(
        name="Same connection",
        source_connection_id=both.id,
        dest_connection_id=both.id,
        source_path="old",
        dest_path="new",
        filters={},
    )
    session.add(job)
    session.commit()
    run = JobRun(
        job_id=job.id,
        trigger=RunTrigger.manual,
        mode=RunMode.dry_run,
        status=RunStatus.success,
        summary={"new": 1},
    )
    session.add(run)
    session.commit()
    session.add(
        JobRunChange(run_id=run.id, action=ChangeAction.new, side=ChangeSide.dest, path="test2.txt")
    )
    session.commit()

    page = authed_client.get(f"/runs/{run.id}").text

    assert "/data/old" in page, "the source subpath should be in the flow label"
    assert "/data/new" in page, "the destination subpath should be in the flow label"
