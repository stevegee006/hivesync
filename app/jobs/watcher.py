"""Continuous mode: keep two endpoints in step without a schedule.

SPEC section 19 lists real-time filesystem watching as a non-goal. This reverses
that decision, and the reversal comes with its limit stated plainly rather than
hidden behind the word "continuous".

**Nothing here is push based, because nothing can be.** Verified against rclone
1.74.4: `ChangeNotify` is false for local, sftp, ftp and smb alike, so no backend
this application supports can announce that something changed. rclone's
`--poll-interval` only does anything for backends that implement it, which none
of ours do. Continuous therefore means polling, and polling every few seconds
against a NAS means walking the whole tree every few seconds.

So the loop backs off. After a cycle that moved something it looks again at the
floor interval, on the assumption that whoever is writing is still writing. After
a cycle that changed nothing it widens towards the ceiling. That keeps a quiet
job cheap and a busy job responsive, which is the best available trade when the
only question you can ask is "has anything changed yet".

Three deliberate constraints:

- **One way only.** bisync lists both sides and carries workdir state, so it is
  both the most expensive thing to run on a loop and the one where a mistake is
  hardest to undo.
- **A quiet period, via rclone's `--min-age`.** A file still being written is
  left alone until it settles, rather than copied half finished and copied again
  next cycle.
- **A check that changed nothing records no run.** A sixty second loop is 1,440
  runs a day; keeping them would bury the runs that matter and make the run
  history useless for the thing it exists for. `Job.last_checked_at` carries the
  proof of life instead.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.jobs import planner
from app.jobs.runner import LiveRunner
from app.models import Job, JobRun, RunMode, RunStatus, RunTrigger, utcnow

logger = logging.getLogger(__name__)


@dataclass
class WatchState:
    """How long to wait before looking at this job again."""

    interval: float
    consecutive_quiet: int = 0


def next_interval(job: Job, state: WatchState, *, changed: bool) -> float:
    """Widen while nothing happens, snap back the moment something does.

    Doubling rather than stepping, because the useful range is wide: a minute
    when someone is actively saving files, a quarter of an hour overnight.
    """
    floor = max(5, job.continuous_interval_seconds)
    ceiling = max(floor, job.continuous_idle_interval_seconds)

    if changed:
        state.consecutive_quiet = 0
        state.interval = float(floor)
        return state.interval

    state.consecutive_quiet += 1
    state.interval = min(float(ceiling), max(float(floor), state.interval * 2))
    return state.interval


def should_keep(run: JobRun) -> bool:
    """Whether a finished continuous run is worth keeping in the history.

    A successful cycle that moved nothing is not history, it is a heartbeat. Any
    failure is kept, including one that failed because the brake refused it:
    that is precisely the run someone needs to find later.
    """
    if run.status != RunStatus.success:
        return True
    return bool(run.files_transferred or run.files_deleted or run.errors_count)


class ContinuousWatcher:
    """Runs the loop for every job in continuous mode.

    One thread for all of them rather than one each: the work happens in the
    LiveRunner's pool, and this only decides when to ask for it.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        runner: LiveRunner,
        *,
        settings: Settings,
        tick_seconds: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._settings = settings
        self._tick = tick_seconds
        self._states: dict[int, WatchState] = {}
        self._due: dict[int, float] = {}
        self._cycle_started: dict[int, datetime] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hivesync-watcher", daemon=True)
        self._thread.start()
        logger.info("Continuous watcher started")

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self._tick):
            try:
                self.tick()
            except Exception:  # a watcher thread must never die silently
                logger.exception("Continuous watcher tick failed")

    # --------------------------------------------------------------------- work

    def tick(self, *, now: float | None = None) -> list[int]:
        """Start any continuous job that is due. Returns the run ids started."""
        import time

        moment = now if now is not None else time.monotonic()
        started: list[int] = []
        session = self._session_factory()
        try:
            jobs = list(
                session.scalars(select(Job).where(Job.continuous.is_(True), Job.enabled.is_(True)))
            )
            live = {job.id for job in jobs}
            for stale in [job_id for job_id in self._states if job_id not in live]:
                self._states.pop(stale, None)
                self._due.pop(stale, None)
                self._cycle_started.pop(stale, None)

            for job in jobs:
                state = self._states.setdefault(
                    job.id, WatchState(interval=float(max(5, job.continuous_interval_seconds)))
                )
                due_at = self._due.setdefault(job.id, moment)
                if moment < due_at:
                    continue

                # Whatever the last cycle did decides how long to wait after
                # this one.
                changed = self._changed_since(session, job, self._cycle_started.get(job.id))
                next_interval(job, state, changed=changed)

                self._cycle_started[job.id] = utcnow()
                run_id = self._start(session, job)
                if run_id is not None:
                    started.append(run_id)
                # Scheduled from the start of the cycle rather than its end: a
                # cycle that takes longer than the interval then runs back to
                # back instead of adding the interval on top of itself.
                self._due[job.id] = moment + state.interval
        finally:
            session.close()
        return started

    def _start(self, session: Session, job: Job) -> int | None:
        try:
            run = planner.create_run(session, job, trigger=RunTrigger.schedule, mode=RunMode.live)
        except planner.RunConflict:
            # Still working through the last cycle. Not worth recording: for a
            # continuous job an overlap is the normal shape of a busy tree, not
            # the anomaly a skipped scheduled run represents.
            logger.debug(
                "Continuous cycle skipped, previous still running", extra={"job": job.name}
            )
            return None

        job.last_checked_at = utcnow()
        session.commit()
        self._runner.submit(run.id)
        return run.id

    def _changed_since(self, session: Session, job: Job, since: datetime | None) -> bool:
        """Whether the last cycle left a run row behind.

        The runner deletes a successful cycle that moved nothing, so a surviving
        run *is* the signal that something happened. That keeps the watcher from
        needing a callback out of the runner, and keeps the two modules from
        importing each other.
        """
        if since is None:
            return False
        return (
            session.scalar(
                select(JobRun.id)
                .where(
                    JobRun.job_id == job.id,
                    JobRun.finished_at.is_not(None),
                    JobRun.finished_at >= since,
                )
                .limit(1)
            )
            is not None
        )
