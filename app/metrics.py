"""Prometheus text output. SPEC section 16.

Hand-rolled rather than pulled from `prometheus_client`, for one reason that
matters: every series here is an aggregate over the `job_run` table, not a
counter held in memory. A process counter resets to zero on restart, and a
container that restarts nightly would report a sawtooth that means nothing. The
database already holds the truth, so the exposition is a query.

The cost is that this module owns the text format, including label escaping. The
format is small and stable, and `test_metrics.py` asserts on it.

Exposed under authentication. Job names, and therefore share and directory names,
appear in the labels. Prometheus scrapes with a bearer token instead: see
`HIVESYNC_METRICS_TOKEN`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, JobRun, RunStatus

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    """Escape a label value. Backslash first, or the other escapes get mangled."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: Sequence[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + inner + "}"


Sample = tuple[Sequence[tuple[str, str]], float]


@dataclass
class _Family:
    name: str
    kind: str
    help_text: str
    samples: Sequence[Sample]

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} {self.kind}"
        for labels, value in self.samples:
            # Integers are rendered without a trailing .0: a counter of 3 reads
            # better than 3.0, and both are valid.
            rendered = str(int(value)) if float(value).is_integer() else repr(float(value))
            yield f"{self.name}{_labels(labels)} {rendered}"


def render(session: Session) -> str:
    """Build the whole exposition from the database."""
    jobs = {job.id: job for job in session.scalars(select(Job))}
    families = [
        *_run_totals(session, jobs),
        *_duration(session, jobs),
        *_counters(session, jobs),
        _last_success(session, jobs),
        _job_info(jobs),
    ]
    lines: list[str] = []
    for family in families:
        lines.extend(family.render())
    # A Prometheus exposition ends with a newline.
    return "\n".join(lines) + "\n"


def _job_labels(jobs: dict[int, Job], job_id: int) -> list[tuple[str, str]]:
    job = jobs.get(job_id)
    return [("job", job.name if job else f"deleted-{job_id}")]


def _run_totals(session: Session, jobs: dict[int, Job]) -> list[_Family]:
    """hivesync_run_total{job,status}.

    Every status is emitted for every job, including zeros. A series that only
    appears once something fails cannot be alerted on before the first failure,
    which is precisely when the alert is wanted.
    """
    counted: dict[tuple[int, RunStatus], int] = {
        (job_id, status): count
        for job_id, status, count in session.execute(
            select(JobRun.job_id, JobRun.status, func.count()).group_by(
                JobRun.job_id, JobRun.status
            )
        )
    }
    samples: list[Sample] = [
        ([*_job_labels(jobs, job_id), ("status", status.value)], counted.get((job_id, status), 0))
        for job_id in jobs
        for status in RunStatus
    ]
    return [
        _Family(
            "hivesync_run_total",
            "counter",
            "Runs recorded, by job and final status.",
            samples,
        )
    ]


def _duration(session: Session, jobs: dict[int, Job]) -> list[_Family]:
    """hivesync_run_duration_seconds, as a summary.

    Only finished runs contribute. A running job has no duration yet, and
    counting it as zero would drag every average toward zero for as long as the
    sync takes, which is exactly when someone is looking.
    """
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    rows = session.execute(
        select(JobRun.job_id, JobRun.started_at, JobRun.finished_at).where(
            JobRun.started_at.is_not(None), JobRun.finished_at.is_not(None)
        )
    )
    for job_id, started, finished in rows:
        if started is None or finished is None:
            continue
        totals[job_id] = totals.get(job_id, 0.0) + (finished - started).total_seconds()
        counts[job_id] = counts.get(job_id, 0) + 1

    return [
        _Family(
            "hivesync_run_duration_seconds_sum",
            "counter",
            "Total seconds spent in finished runs, by job.",
            [([*_job_labels(jobs, job_id)], totals.get(job_id, 0.0)) for job_id in jobs],
        ),
        _Family(
            "hivesync_run_duration_seconds_count",
            "counter",
            "Finished runs contributing to the duration total, by job.",
            [([*_job_labels(jobs, job_id)], counts.get(job_id, 0)) for job_id in jobs],
        ),
    ]


_COUNTER_COLUMNS = (
    ("hivesync_files_transferred_total", JobRun.files_transferred, "Files transferred, by job."),
    (
        "hivesync_files_deleted_total",
        JobRun.files_deleted,
        "Files removed from the destination, by job. Includes archived files, "
        "which leave the destination too.",
    ),
    (
        "hivesync_files_archived_total",
        JobRun.files_archived,
        "Files moved into the archive, by job.",
    ),
    ("hivesync_bytes_transferred_total", JobRun.bytes_transferred, "Bytes transferred, by job."),
)


def _counters(session: Session, jobs: dict[int, Job]) -> list[_Family]:
    families: list[_Family] = []
    for name, column, help_text in _COUNTER_COLUMNS:
        # Live runs only. A dry run transfers nothing, and including it would
        # make the counters depend on how often someone clicks Preview.
        # Not dict(session.execute(...)): Result has a keys() method, so dict()
        # takes the mapping path, subscripts the Result and raises. It reads as
        # though it works right up until it runs.
        totals: dict[int, int] = {}
        for job_id, total in session.execute(
            select(JobRun.job_id, func.coalesce(func.sum(column), 0))
            .where(JobRun.mode == "live")
            .group_by(JobRun.job_id)
        ):
            totals[job_id] = total
        families.append(
            _Family(
                name,
                "counter",
                help_text,
                [([*_job_labels(jobs, job_id)], totals.get(job_id, 0)) for job_id in jobs],
            )
        )
    return families


def _last_success(session: Session, jobs: dict[int, Job]) -> _Family:
    """hivesync_last_success_timestamp{job}, in Unix seconds.

    Zero means "never succeeded", which is a real and alertable state, so the
    series is emitted rather than omitted.
    """
    latest: dict[int, datetime | None] = {}
    for job_id, finished in session.execute(
        select(JobRun.job_id, func.max(JobRun.finished_at))
        .where(JobRun.status == RunStatus.success, JobRun.mode == "live")
        .group_by(JobRun.job_id)
    ):
        latest[job_id] = finished
    samples: list[Sample] = []
    for job_id in jobs:
        finished = latest.get(job_id)
        samples.append(([*_job_labels(jobs, job_id)], finished.timestamp() if finished else 0.0))
    return _Family(
        "hivesync_last_success_timestamp",
        "gauge",
        "Unix time of the last successful live run, by job. Zero means never.",
        samples,
    )


def _job_info(jobs: dict[int, Job]) -> _Family:
    """Whether a job is even scheduled to run.

    Not in SPEC 16, but without it "last success was three days ago" cannot be
    distinguished from "someone disabled this job three days ago", and those need
    different responses.
    """
    return _Family(
        "hivesync_job_enabled",
        "gauge",
        "1 when the job is enabled, 0 when it is disabled.",
        [([("job", job.name)], 1 if job.enabled else 0) for job in jobs.values()],
    )
