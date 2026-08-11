"""The history screen: every file this instance has moved.

The run pages answer "what did this run do". This answers "what happened to
this file", which is the question someone has when something is missing, and it
is the only screen that spans jobs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine
from app.models import (
    ChangeAction,
    ChangeSide,
    Connection,
    ConnectionType,
    Job,
    JobRun,
    JobRunChange,
    RunMode,
    RunStatus,
    RunTrigger,
)


def _history(settings) -> dict[str, int]:
    """Two jobs, two runs, a handful of changes across both."""
    session = sessionmaker(bind=create_db_engine(settings))()
    source = Connection(name="UltraCC", type=ConnectionType.local, base_path="/src")
    dest = Connection(name="Synology", type=ConnectionType.local, base_path="/dst")
    session.add_all([source, dest])
    session.commit()

    movies = Job(
        name="Movies", source_connection_id=source.id, dest_connection_id=dest.id, filters={}
    )
    shows = Job(
        name="TV Shows", source_connection_id=source.id, dest_connection_id=dest.id, filters={}
    )
    session.add_all([movies, shows])
    session.commit()

    when = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    first = JobRun(
        job_id=movies.id,
        trigger=RunTrigger.manual,
        mode=RunMode.live,
        status=RunStatus.success,
        started_at=when,
        finished_at=when,
    )
    second = JobRun(
        job_id=shows.id,
        trigger=RunTrigger.schedule,
        mode=RunMode.live,
        status=RunStatus.success,
        started_at=when + timedelta(hours=1),
        finished_at=when + timedelta(hours=1),
    )
    session.add_all([first, second])
    session.commit()

    session.add_all(
        [
            JobRunChange(
                run_id=first.id,
                action=ChangeAction.new,
                side=ChangeSide.dest,
                path="Robin Hood.mkv",
                size=3033782870,
                peak_speed_bps=41952494.9,
            ),
            JobRunChange(
                run_id=first.id,
                action=ChangeAction.deleted,
                side=ChangeSide.dest,
                path="Old Film.mkv",
                size=100,
            ),
            JobRunChange(
                run_id=second.id,
                action=ChangeAction.archived,
                side=ChangeSide.dest,
                path="Series/S01E01.mkv",
                size=500,
            ),
        ]
    )
    session.commit()
    return {"movies": movies.id, "shows": shows.id, "first": first.id, "second": second.id}


def test_every_kind_of_change_is_listed(authed_client: TestClient, settings) -> None:
    _history(settings)

    page = authed_client.get("/history").text

    assert "Robin Hood.mkv" in page
    assert "Old Film.mkv" in page
    assert "Series/S01E01.mkv" in page
    # Across jobs, which is the point: no other screen spans them.
    assert "Movies" in page
    assert "TV Shows" in page


def test_the_columns_asked_for_are_there(authed_client: TestClient, settings) -> None:
    ids = _history(settings)

    page = authed_client.get("/history").text

    assert ">Job<" in page or "Job" in page
    assert f"#{ids['first']}" in page
    assert "Synology" in page, "the location column should name the endpoint"
    assert "2.8 GB" in page, "size should be readable, not a byte count"
    assert "40.0 MB/s" in page, "the peak speed should be shown"


def test_a_speed_nobody_measured_is_blank_not_zero(authed_client: TestClient, settings) -> None:
    """A deletion transfers nothing, and a small file can start and finish
    between two stats ticks. Rendering that as 0 B/s claims a measurement that
    was never taken."""
    _history(settings)

    page = authed_client.get("/history").text
    # Scoped to the table: the activity strip at the bottom of every page shows
    # 0 B/s when nothing is running, which is correct there and would otherwise
    # make this assertion pass or fail for the wrong reason.
    table = page[page.index("<table") : page.index("</table>")]

    assert "0 B/s" not in table
    # The measured one is still shown.
    assert "40.0 MB/s" in table


def test_searching_narrows_to_matching_files(authed_client: TestClient, settings) -> None:
    _history(settings)

    page = authed_client.get("/history?q=Robin").text

    assert "Robin Hood.mkv" in page
    assert "Old Film.mkv" not in page
    assert "S01E01" not in page


def test_searching_matches_the_job_name_too(authed_client: TestClient, settings) -> None:
    """ "Everything that job touched" is as common a question as one file."""
    _history(settings)

    page = authed_client.get("/history?q=TV Shows").text

    assert "S01E01" in page
    assert "Robin Hood.mkv" not in page


def test_sorting_by_size_orders_the_rows(authed_client: TestClient, settings) -> None:
    _history(settings)

    descending = authed_client.get("/history?sort=size&direction=desc").text
    ascending = authed_client.get("/history?sort=size&direction=asc").text

    assert descending.index("Robin Hood.mkv") < descending.index("Old Film.mkv")
    assert ascending.index("Old Film.mkv") < ascending.index("Robin Hood.mkv")


def test_an_unknown_sort_column_falls_back_rather_than_reaching_the_query(
    authed_client: TestClient, settings
) -> None:
    """The sort key is matched against a fixed map, so a column name arriving in
    a URL can never become an ORDER BY."""
    _history(settings)

    response = authed_client.get("/history?sort=path%3B+DROP+TABLE+job_run_change")

    assert response.status_code == 200
    assert "Robin Hood.mkv" in response.text


def test_the_search_survives_a_sort(authed_client: TestClient, settings) -> None:
    """Sorting a filtered list must not quietly drop the filter."""
    _history(settings)

    page = authed_client.get("/history?q=Robin&sort=size&direction=asc").text

    assert "Robin Hood.mkv" in page
    assert "Old Film.mkv" not in page
    # And the header links carry it, so the next click keeps it too.
    assert "q=Robin" in page


def test_history_is_reachable_from_every_page(authed_client: TestClient) -> None:
    page = authed_client.get("/").text

    assert 'href="/history"' in page
    assert 'aria-label="History"' in page


def test_an_empty_history_says_so(authed_client: TestClient) -> None:
    page = authed_client.get("/history").text

    assert "Nothing has been copied, deleted or archived yet" in page
