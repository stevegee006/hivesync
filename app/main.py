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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
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
from app.logging_conf import configure_logging
from app.models import SECRET_KEY_FINGERPRINT, Setting

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    app.state.binaries = binaries.collect(settings.expected_rclone_version)
    report: binaries.BinaryReport = app.state.binaries
    if not report.rclone.ok:
        logger.error("rclone is not usable", extra={"error": report.rclone.error})
    if not report.lftp.ok:
        logger.error("lftp is not usable", extra={"error": report.lftp.error})
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

    logger.info(
        "HiveSync started",
        extra={
            "app_version": __version__,
            "rclone_version": report.rclone.version,
            "lftp_version": report.lftp.version,
            "auth_mode": settings.auth_mode,
            "timezone": settings.timezone,
        },
    )
    try:
        yield
    finally:
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
