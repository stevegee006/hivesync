"""Scheduled runs.

The schedule lives in the Job table, not in an APScheduler jobstore. SPEC
section 3 specifies SQLAlchemyJobStore, and this deviates deliberately:

- That store creates an `apscheduler_jobs` table in our database which is not in
  Base.metadata, so Alembic autogenerate proposes dropping it. Verified: a
  freshly started jobstore produces `remove_table apscheduler_jobs` in the diff.
  The next generated migration would delete the schedule store on upgrade.
- It is a second copy of every schedule, which must be kept in step with the Job
  table on every edit and delete, and it pickles a function reference that breaks
  when the function moves.

Rebuilding the schedule from the Job table at startup gives the same restart
survival, because the schedule was always persisted in `Job.schedule_cron`. It
also means a restart fires no backlog at all, which is what SPEC section 9 asks
`coalesce` and `misfire_grace_time` to achieve.

Overlap is prevented in three independent places, because "never double-runs" is
a claim about a tool that deletes files:

1. APScheduler's `max_instances=1`, so a trigger cannot fire concurrently.
2. This module records a skipped run rather than queueing, per SPEC 6.2.
3. The database's partial unique index refuses a second active row regardless.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import preferences
from app.config import Settings
from app.jobs import cron, planner, retention
from app.jobs.runner import LiveRunner
from app.models import Job, JobRun, RunMode, RunStatus, RunTrigger, utcnow

logger = logging.getLogger(__name__)

# SPEC section 9. A fire that is later than this is dropped rather than run, so
# a container that was down overnight does not start a sync at breakfast.
DEFAULT_MISFIRE_GRACE_SECONDS = 300


MAINTENANCE_KEY = "hivesync-maintenance"
# Daily, at a time nothing else is scheduled for. Retention deletes archived
# files, so it runs on its own rather than sharing a fire with a sync.
MAINTENANCE_HOUR = 3
MAINTENANCE_MINUTE = 17


def job_key(job_id: int) -> str:
    return f"hivesync-job-{job_id}"


class JobScheduler:
    """Owns the APScheduler instance and keeps it in step with the Job table."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        runner: LiveRunner,
        *,
        settings: Settings,
        misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._settings = settings
        self._misfire_grace = misfire_grace_seconds
        self._scheduler = BackgroundScheduler(timezone=settings.timezone or "UTC")

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._scheduler.start()
        self.reload()
        logger.info("Scheduler started", extra={"jobs": len(self._scheduler.get_jobs())})

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # ------------------------------------------------------------------ sync

    def reload(self) -> None:
        """Rebuild the whole schedule from the Job table.

        Called at startup and after any change to a job. Cheap: a homelab has
        tens of jobs, not thousands, and rebuilding wholesale removes every
        opportunity for the scheduler and the database to disagree.
        """
        session = self._session_factory()
        try:
            jobs = list(session.scalars(select(Job)))
        finally:
            session.close()

        self._scheduler.remove_all_jobs()
        for job in jobs:
            self._register(job)
        self._register_maintenance()

    def _register_maintenance(self) -> None:
        """The daily housekeeping pass: retention, logs, run history.

        Registered here rather than in start() because reload() clears every job,
        so a job edit would otherwise silently unschedule maintenance until the
        next restart.
        """
        self._scheduler.add_job(
            self._maintain,
            trigger=CronTrigger(
                hour=MAINTENANCE_HOUR,
                minute=MAINTENANCE_MINUTE,
                timezone=self._settings.timezone or "UTC",
            ),
            id=MAINTENANCE_KEY,
            name="Maintenance",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=self._misfire_grace,
            max_instances=1,
        )

    def _maintain(self) -> None:
        session = self._session_factory()
        try:
            retention.run(session, self._settings, preferences.load(session))
        except Exception:  # a scheduler thread must never die silently
            logger.exception("Maintenance pass failed")
        finally:
            session.close()

    def _register(self, job: Job) -> None:
        if not job.enabled or not (job.schedule_cron or "").strip():
            return
        try:
            trigger = cron.build_trigger(job.schedule_cron or "", job.timezone or "UTC")
        except cron.CronError as exc:
            # A job saved before validation existed, or edited in the database by
            # hand. Skip it loudly rather than refusing to start the scheduler and
            # taking every other job down with it.
            logger.error(
                "Job has an unusable schedule and will not run",
                extra={"job": job.name, "cron": job.schedule_cron, "error": str(exc)},
            )
            return

        self._scheduler.add_job(
            self._fire,
            trigger=trigger,
            args=[job.id],
            id=job_key(job.id),
            name=job.name,
            replace_existing=True,
            # SPEC 9: never run a backlog. One fire, and only if it is recent.
            coalesce=True,
            misfire_grace_time=self._misfire_grace,
            # One instance per job. The first of three overlap defences.
            max_instances=1,
        )

    def next_run_time(self, job_id: int) -> datetime | None:
        scheduled = self._scheduler.get_job(job_key(job_id))
        return scheduled.next_run_time if scheduled else None

    # ------------------------------------------------------------------ firing

    def _fire(self, job_id: int) -> None:
        """Start a scheduled live run, or record why it did not start."""
        session = self._session_factory()
        try:
            job = session.get(Job, job_id)
            if job is None or not job.enabled:
                return

            try:
                run = planner.create_run(
                    session, job, trigger=RunTrigger.schedule, mode=RunMode.live
                )
            except planner.RunConflict:
                # SPEC 6.2: record a skipped run rather than queueing indefinitely.
                # Silently dropping it would leave an operator wondering why a
                # scheduled sync never happened.
                _record_skip(session, job, "A previous run was still in progress.")
                return

            logger.info("Scheduled run starting", extra={"job": job.name, "run_id": run.id})
            self._runner.submit(run.id)
        except Exception:  # a scheduler thread must never die silently
            logger.exception("Scheduled fire failed", extra={"job_id": job_id})
        finally:
            session.close()


def _record_skip(session: Session, job: Job, reason: str) -> None:
    """A visible record that a scheduled run did not happen, and why."""
    session.add(
        JobRun(
            job_id=job.id,
            trigger=RunTrigger.schedule,
            mode=RunMode.live,
            status=RunStatus.skipped,
            started_at=utcnow(),
            finished_at=utcnow(),
            skip_reason=reason,
        )
    )
    session.commit()
    logger.warning("Scheduled run skipped", extra={"job": job.name, "reason": reason})
