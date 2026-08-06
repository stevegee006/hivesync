"""Live activity for the dashboard: one endpoint, one connection.

Deliberately polled rather than streamed. The dashboard shows every job at once,
and an SSE stream per card would open one connection per running job on top of
the page itself. Browsers allow about six per host, and this application has
already been brought to a halt once by leaking those, so the whole strip is fed
by a single request every couple of seconds.

**rclone reports no up/down split.** Its stats carry one `speed` and one `bytes`
figure; the direction is a property of the job, not of the transfer. So the split
here is derived from where the job writes: to a remote destination is outbound,
from a remote source to a local one is inbound, and local to local is neither.

A job between **two remotes** counts towards both, because that is what is
physically happening: the bytes arrive from one endpoint and leave for the other
through this machine, so it really is receiving and sending at that rate. It is
also the common shape here, and reporting it as neither left the network panel
reading zero while a transfer was plainly running.

`total_speed` is the sum of the runs themselves rather than of the two
directions, so a remote-to-remote transfer is not counted twice in the total.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.engines.parsers import TransferStats
from app.jobs.events import activity
from app.models import Connection, ConnectionType, Job, JobRun, RunStatus

router = APIRouter(tags=["activity"])

# The chart windows the UI offers, in seconds.
WINDOWS = {"1m": 60, "10m": 600, "1h": 3600}


class ActiveRun(BaseModel):
    run_id: int
    job_id: int
    job_name: str
    mode: str
    percentage: int
    bytes_done: int
    total_bytes: int
    speed: float
    eta_seconds: int | None
    current_file: str | None
    direction: str  # "up", "down" or "local"


class ActivityResponse(BaseModel):
    running: list[ActiveRun]
    total_speed: float
    up_speed: float
    down_speed: float
    samples: list[float]
    sample_seconds: int
    session_bytes: int
    session_max_speed: float
    lifetime_bytes: int
    last_synced: str | None


def direction_of(job: Job) -> str:
    """Which way the bytes are going, from the job rather than from rclone.

    A remote destination means data leaving this machine, a remote source
    feeding a local destination means data arriving. Anything else is local
    movement and is labelled as such rather than guessed at.
    """
    source: Connection | None = job.source_connection
    dest: Connection | None = job.dest_connection
    if source is None or dest is None:
        return "local"
    if job.direction.value == "dest_to_source":
        source, dest = dest, source

    source_remote = source.type != ConnectionType.local
    dest_remote = dest.type != ConnectionType.local
    if dest_remote and not source_remote:
        return "up"
    if source_remote and not dest_remote:
        return "down"
    if source_remote and dest_remote:
        # Both ends are remote, so the bytes come in and go out again. Counting
        # them as one direction would halve or double the figure depending on
        # which was picked, so neither is.
        return "both"
    return "local"


@router.post("/activity/reset-session", status_code=204)
def reset_session(_user: CurrentUser) -> None:
    """Zero the session counters. Lifetime, which lives in the database, is
    untouched."""
    activity.reset_session()


@router.get("/activity", response_model=ActivityResponse)
def read_activity(_user: CurrentUser, session: DbSession, window: str = "1m") -> ActivityResponse:
    seconds = WINDOWS.get(window, 60)

    live = activity.active()
    runs: list[ActiveRun] = []
    up = down = 0.0

    if live:
        rows = session.execute(
            select(JobRun, Job).join(Job, Job.id == JobRun.job_id).where(JobRun.id.in_(live))
        ).all()
        for run, job in rows:
            stats = live.get(run.id)
            if not isinstance(stats, TransferStats):
                continue
            heading = direction_of(job)
            if heading in ("up", "both"):
                up += stats.speed
            if heading in ("down", "both"):
                down += stats.speed

            busiest = max(stats.transferring, key=lambda item: item.speed, default=None)
            runs.append(
                ActiveRun(
                    run_id=run.id,
                    job_id=job.id,
                    job_name=job.name,
                    mode=run.mode.value,
                    percentage=stats.percentage,
                    bytes_done=stats.bytes_done,
                    total_bytes=stats.total_bytes,
                    speed=stats.speed,
                    eta_seconds=stats.eta_seconds,
                    current_file=busiest.name if busiest else None,
                    direction=heading,
                )
            )

    samples = [sample.speed for sample in activity.samples(since_seconds=seconds)]
    session_bytes, session_max = activity.session()

    # Lifetime comes from the database rather than memory: it is the one figure
    # here that should survive a restart.
    lifetime = int(
        session.scalar(
            select(func.coalesce(func.sum(JobRun.bytes_transferred), 0)).where(
                JobRun.mode == "live"
            )
        )
        or 0
    )
    latest = session.scalar(
        select(func.max(JobRun.finished_at)).where(JobRun.status == RunStatus.success)
    )

    return ActivityResponse(
        running=sorted(runs, key=lambda item: item.job_name),
        total_speed=sum(item.speed for item in runs),
        up_speed=up,
        down_speed=down,
        samples=samples,
        sample_seconds=seconds,
        session_bytes=session_bytes,
        session_max_speed=session_max,
        lifetime_bytes=lifetime,
        last_synced=latest.isoformat() if latest else None,
    )
