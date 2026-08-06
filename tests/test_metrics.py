"""The Prometheus exposition. SPEC section 16.

Acceptance criterion: /metrics parses as Prometheus text format, and the counters
move by exactly one run's counts.

The parser below is deliberate. Asserting that a substring appears in the output
would pass for text that no scraper can read, and this module owns the format by
hand, so the format is what needs testing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import metrics
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

AT = datetime(2026, 8, 5, 2, 30, tzinfo=UTC)


def parse(text: str) -> dict[str, float]:
    """A minimal Prometheus text parser, keyed by the full series name."""
    series: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        assert name, f"unparseable line: {line!r}"
        series[name] = float(value)
    return series


def types_declared(text: str) -> dict[str, str]:
    return {
        parts[2]: parts[3]
        for parts in (line.split() for line in text.splitlines())
        if len(parts) == 4 and parts[0] == "#" and parts[1] == "TYPE"
    }


@pytest.fixture
def db(settings: Settings, tmp_path: Path):
    from tests.conftest import create_schema

    create_schema(settings)
    return sessionmaker(bind=create_db_engine(settings))()


def _job(session, name: str = "Nightly Media", **overrides) -> Job:  # noqa: ANN003
    source = Connection(name=f"{name}-src", type=ConnectionType.local, base_path="/s")
    dest = Connection(name=f"{name}-dst", type=ConnectionType.local, base_path="/d")
    session.add_all([source, dest])
    session.commit()
    job = Job(
        name=name,
        source_connection_id=source.id,
        dest_connection_id=dest.id,
        filters={},
        **overrides,
    )
    session.add(job)
    session.commit()
    return job


def _run(session, job: Job, **overrides) -> JobRun:  # noqa: ANN003
    fields: dict = {
        "job_id": job.id,
        "trigger": RunTrigger.schedule,
        "mode": RunMode.live,
        "status": RunStatus.success,
        "started_at": AT,
        "finished_at": AT + timedelta(seconds=10),
        "files_transferred": 3,
        "files_deleted": 1,
        "files_archived": 1,
        "bytes_transferred": 1024,
    }
    fields.update(overrides)
    run = JobRun(**fields)
    session.add(run)
    session.commit()
    return run


# --------------------------------------------------------------------------
# Format
# --------------------------------------------------------------------------


def test_output_parses_and_declares_every_series(db) -> None:
    job = _job(db)
    _run(db, job)
    text = metrics.render(db)

    assert text.endswith("\n")
    series = parse(text)
    assert series, "no samples were emitted"

    declared = types_declared(text)
    # Every family carries HELP and TYPE, which is what makes it self describing.
    for name in series:
        family = name.split("{")[0]
        assert family in declared, f"{family} has no # TYPE line"
        assert declared[family] in ("counter", "gauge")


def test_every_series_spec_16_names_is_present(db) -> None:
    job = _job(db)
    _run(db, job)
    series = parse(metrics.render(db))
    names = {name.split("{")[0] for name in series}
    assert {
        "hivesync_run_total",
        "hivesync_run_duration_seconds_sum",
        "hivesync_run_duration_seconds_count",
        "hivesync_files_transferred_total",
        "hivesync_bytes_transferred_total",
        "hivesync_files_deleted_total",
        "hivesync_files_archived_total",
        "hivesync_last_success_timestamp",
    } <= names


def test_a_job_name_with_quotes_and_backslashes_is_escaped(db) -> None:
    """A job name is free text and lands in a label. An unescaped quote produces
    a line no scraper can read, and the failure is at scrape time, far from here."""
    job = _job(db, name='weird "quoted" \\ name')
    _run(db, job)
    text = metrics.render(db)

    assert '\\"quoted\\"' in text
    assert "\\\\ name" in text
    # And it still parses.
    assert parse(text)


def test_a_job_that_has_never_run_still_reports_zeros(db) -> None:
    """A series that only appears after the first failure cannot be alerted on
    before the first failure, which is when the alert is wanted."""
    _job(db, name="Fresh")
    series = parse(metrics.render(db))
    assert series['hivesync_run_total{job="Fresh",status="failed"}'] == 0
    assert series['hivesync_last_success_timestamp{job="Fresh"}'] == 0
    assert series['hivesync_files_transferred_total{job="Fresh"}'] == 0


# --------------------------------------------------------------------------
# The criterion: counters move by exactly one run
# --------------------------------------------------------------------------


def test_counters_move_by_exactly_one_runs_counts(db) -> None:
    job = _job(db)
    _run(db, job)
    before = parse(metrics.render(db))

    _run(db, job, files_transferred=5, files_deleted=2, files_archived=2, bytes_transferred=4096)
    after = parse(metrics.render(db))

    label = 'job="Nightly Media"'
    assert (
        after[f"hivesync_files_transferred_total{{{label}}}"]
        - before[f"hivesync_files_transferred_total{{{label}}}"]
        == 5
    )
    assert (
        after[f"hivesync_files_deleted_total{{{label}}}"]
        - before[f"hivesync_files_deleted_total{{{label}}}"]
        == 2
    )
    assert (
        after[f"hivesync_files_archived_total{{{label}}}"]
        - before[f"hivesync_files_archived_total{{{label}}}"]
        == 2
    )
    assert (
        after[f"hivesync_bytes_transferred_total{{{label}}}"]
        - before[f"hivesync_bytes_transferred_total{{{label}}}"]
        == 4096
    )
    assert after[f'hivesync_run_total{{{label},status="success"}}'] == 2


def test_dry_runs_do_not_move_the_transfer_counters(db) -> None:
    """Otherwise the counters depend on how often someone clicks Preview."""
    job = _job(db)
    _run(db, job)
    before = parse(metrics.render(db))

    _run(db, job, mode=RunMode.dry_run, files_transferred=99, bytes_transferred=99)
    after = parse(metrics.render(db))

    label = 'job="Nightly Media"'
    assert (
        after[f"hivesync_files_transferred_total{{{label}}}"]
        == before[f"hivesync_files_transferred_total{{{label}}}"]
    )
    # The run itself is still counted: it happened.
    assert after[f'hivesync_run_total{{{label},status="success"}}'] == 2


def test_duration_counts_only_finished_runs(db) -> None:
    job = _job(db)
    _run(db, job)
    _run(db, job, status=RunStatus.running, finished_at=None)

    series = parse(metrics.render(db))
    label = 'job="Nightly Media"'
    assert series[f"hivesync_run_duration_seconds_count{{{label}}}"] == 1
    assert series[f"hivesync_run_duration_seconds_sum{{{label}}}"] == 10


def test_last_success_ignores_a_later_failure(db) -> None:
    job = _job(db)
    _run(db, job)
    _run(db, job, status=RunStatus.failed, finished_at=AT + timedelta(hours=1))

    series = parse(metrics.render(db))
    stamp = series['hivesync_last_success_timestamp{job="Nightly Media"}']
    assert stamp == (AT + timedelta(seconds=10)).timestamp()


def test_a_disabled_job_is_distinguishable_from_a_stalled_one(db) -> None:
    _job(db, name="Paused", enabled=False)
    series = parse(metrics.render(db))
    assert series['hivesync_job_enabled{job="Paused"}'] == 0


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


def test_metrics_needs_authentication(client: TestClient) -> None:
    """Job names are share names. This endpoint is never open."""
    response = client.get("/metrics")
    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")
    # A scraper cannot follow a redirect to an HTML login form, so it must not
    # get one.
    assert "<html" not in response.text.lower()


def test_a_session_can_read_metrics(authed_client: TestClient) -> None:
    response = authed_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_a_bearer_token_can_scrape_without_a_session(tmp_path: Path) -> None:
    from app.main import create_app
    from tests.conftest import create_schema, make_settings

    settings = make_settings(tmp_path, metrics_token="scrape-me")
    create_schema(settings)
    with TestClient(create_app(settings)) as client:
        assert client.get("/metrics").status_code == 401
        ok = client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
        assert ok.status_code == 200
        wrong = client.get("/metrics", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
