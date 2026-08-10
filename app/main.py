"""FastAPI application factory and process entry point.

Startup order matters and is deliberate:

1. Validate the encryption key before anything else. Without it every stored
   credential is unreadable, so starting up would only defer the failure.
2. Compare the key fingerprint against the one recorded at first boot. A swapped
   key is reported here, in plain language, rather than surfacing later as an
   opaque decrypt failure during a scheduled run.
3. Create the bootstrap admin if the user table is empty.

Schema migrations are not run here. The entrypoint runs `alembic upgrade head`
before the server starts, so a failed migration stops the container instead of
leaving a half-migrated database serving requests.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app import __version__, binaries, crypto, csrf, filter_presets, security, web
from app.api import api_router
from app.api import metrics as metrics_api
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory, session_scope
from app.headers import SecurityHeadersMiddleware
from app.jobs.planner import PlanRunner
from app.jobs.runner import LiveRunner
from app.jobs.scheduler import JobScheduler
from app.jobs.watcher import ContinuousWatcher
from app.logging_conf import configure_logging
from app.models import SECRET_KEY_FINGERPRINT, JobRun, RunStatus, Setting, utcnow

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """A configuration problem that must stop the process, with a message written
    for a human rather than a stack trace reader."""


def _check_key_fingerprint(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    expected = crypto.key_fingerprint(settings.secret_key)
    with session_scope(app.state.session_factory) as session:
        row = session.get(Setting, SECRET_KEY_FINGERPRINT)
        if row is None:
            session.add(Setting(key=SECRET_KEY_FINGERPRINT, value=expected))
            logger.info("Recorded encryption key fingerprint for this database")
            return
        if row.value != expected:
            raise StartupError(
                "HIVESYNC_SECRET_KEY does not match the key this database was "
                "created with, so no stored credential can be decrypted. Restore "
                "the original key, or, if it is lost, delete the credentials in "
                "the UI and re-enter them after clearing the "
                f"'{SECRET_KEY_FINGERPRINT}' row from the setting table."
            )


def _release_interrupted_runs(session: Session) -> None:
    """Close out runs that a restart killed mid-flight.

    A row left saying "running" describes a process that no longer exists, and
    nothing else will ever finish it. The partial unique index then refuses a new
    run for that job forever: a scheduled job silently records skip after skip,
    and a continuous job stops looking altogether with nothing in the log to say
    why. Found exactly that way, by restarting the container during a sync.

    Recorded as failed rather than quietly deleted. Work may well have happened
    on disk before the process died, and the next run's delete brake reads the
    resulting state, so pretending it never ran would be a lie about the tree.
    """
    interrupted = list(
        session.scalars(
            select(JobRun).where(JobRun.status.in_([RunStatus.queued, RunStatus.running]))
        )
    )
    if not interrupted:
        return

    for run in interrupted:
        run.status = RunStatus.failed
        run.finished_at = utcnow()
        run.errors_count = max(run.errors_count, 1)
        run.summary = {
            "error": (
                "This run was interrupted by a restart, so how much of it "
                "completed is unknown. Check the destination, then run it again."
            )
        }
    session.commit()
    logger.warning("Released runs left behind by a restart", extra={"count": len(interrupted)})


def run_startup_checks(app: FastAPI) -> None:
    """Validate persistent state before the server accepts a request.

    Called from create_app rather than from the lifespan handler so a failure
    surfaces as a readable message from main() instead of a traceback out of
    uvicorn's startup. The database schema is expected to exist already: the
    entrypoint runs `alembic upgrade head` first.
    """
    settings: Settings = app.state.settings
    _check_key_fingerprint(app)
    with session_scope(app.state.session_factory) as session:
        security.bootstrap_admin(session, settings)
        filter_presets.seed_builtin_presets(session)
        _release_interrupted_runs(session)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    app.state.binaries = binaries.collect(settings.expected_rclone_version)
    report: binaries.BinaryReport = app.state.binaries
    if not report.rclone.ok:
        logger.error("rclone is not usable", extra={"error": report.rclone.error})
    if report.rclone_matches_expected is False:
        logger.error(
            "Installed rclone is not the pinned version",
            extra={
                "installed": report.rclone.version,
                "expected": report.expected_rclone_version,
            },
        )

    if settings.auth_mode == "none":
        logger.warning(
            "Authentication is disabled. Every request acts as the admin account. "
            "This is only appropriate behind a trusted reverse proxy that "
            "authenticates for you."
        )

    # The schedule is rebuilt from the Job table rather than loaded from a
    # jobstore, so restart survival comes from our own database. See
    # app/jobs/scheduler.py.
    app.state.scheduler.start()
    app.state.watcher.start()

    logger.info(
        "HiveSync started",
        extra={
            "app_version": __version__,
            "rclone_version": report.rclone.version,
            "auth_mode": settings.auth_mode,
            "timezone": settings.timezone,
        },
    )
    try:
        yield
    finally:
        app.state.watcher.shutdown()
        app.state.scheduler.shutdown()
        app.state.live_runner.shutdown()
        app.state.plan_runner.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Raises CryptoKeyError if the key is missing or malformed.
    crypto.validate_key(settings.secret_key)
    settings.ensure_directories()

    app = FastAPI(
        title="HiveSync",
        version=__version__,
        description="File sync orchestrator built on rclone.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db_engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    app.state.secrets = crypto.SecretBox(settings.secret_key)
    # Probed again in lifespan. Set here so /api/health is answerable even if a
    # test client never enters the lifespan context.
    app.state.binaries = binaries.collect(settings.expected_rclone_version)
    app.state.plan_runner = PlanRunner(
        app.state.session_factory,
        box=app.state.secrets,
        settings=settings,
        max_workers=settings.max_concurrent_runs,
    )
    app.state.live_runner = LiveRunner(
        app.state.session_factory,
        box=app.state.secrets,
        settings=settings,
        max_workers=settings.max_concurrent_runs,
    )
    # Started in the lifespan, not here: create_app runs during tests that never
    # want a background thread firing real syncs.
    app.state.scheduler = JobScheduler(
        app.state.session_factory, app.state.live_runner, settings=settings
    )
    # Continuous jobs are driven here rather than by the scheduler: their timing
    # is a backoff computed from what the last cycle did, which a cron trigger
    # cannot express. See app/jobs/watcher.py.
    app.state.watcher = ContinuousWatcher(
        app.state.session_factory, app.state.live_runner, settings=settings
    )

    # Middleware runs outermost-last, so the session must be added after CSRF for
    # the CSRF check to see a decoded session. Getting this order wrong means the
    # token is never found and every form breaks, which is at least loud.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(csrf.CsrfMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=crypto.derive_session_secret(settings.secret_key),
        session_cookie="hivesync_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    app.include_router(api_router, prefix="/api")
    # At the root, not under /api: this is where a Prometheus scrape config
    # looks. See app/api/metrics.py for why it still requires authentication.
    app.include_router(metrics_api.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=str(web.STATIC_DIR)), name="static")

    @app.exception_handler(security.NotAuthenticated)
    async def _handle_unauthenticated(
        request: Request, _exc: security.NotAuthenticated
    ) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        # An unclaimed instance has nothing to sign in to, so sending someone to
        # a login form would be a dead end: no account exists and none can be
        # created from there.
        factory: sessionmaker[Session] = request.app.state.session_factory
        with factory() as session, suppress(Exception):
            if security.needs_setup(session):
                return RedirectResponse(url="/setup", status_code=303)
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=f"/login?next={target}", status_code=303)

    run_startup_checks(app)
    return app


def main() -> int:
    """Console entry point. Turns configuration errors into readable output."""
    import uvicorn

    try:
        settings = get_settings()
        app = create_app(settings)
    except crypto.CryptoKeyError as exc:
        print(f"\nHiveSync cannot start.\n\n{exc}\n", file=sys.stderr)
        print(f"Suggested value:\n\n  {crypto.generate_key_suggestion()}\n", file=sys.stderr)
        return 1
    except (StartupError, security.BootstrapError) as exc:
        print(f"\nHiveSync cannot start.\n\n{exc}\n", file=sys.stderr)
        return 1

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # logging_conf owns the handlers
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
