"""Liveness, and a separate authenticated endpoint for version detail.

**Split at M8, and this is a deliberate change to something M0 asserted.** Until
now `/api/health` reported the rclone and lftp versions to anyone who could reach
the port, which is a free inventory of the binaries to attack. It stays
unauthenticated because the container HEALTHCHECK calls it and has no session,
but it now answers only "is this process serving and can it reach its database".

`/api/health/detail` carries the version report and needs a session or the API
token. M0's acceptance criterion, that health reports the pinned versions, is
still checked in CI: the workflow signs in first.

Status semantics, unchanged:
- database unreachable, 503. The app cannot function and the container should be
  restarted.
- a missing or unexpected binary, 200 with status "degraded". The UI stays
  reachable so an operator can see what is wrong, which a restart loop prevents.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app import __version__
from app.api.deps import CurrentUser
from app.binaries import BinaryReport
from app.db import database_ok
from app.schemas.health import (
    BinaryHealth,
    DatabaseHealth,
    HealthResponse,
    HealthStatus,
    LivenessResponse,
    RcloneHealth,
)

router = APIRouter(tags=["health"])


def _status(db_ok: bool, report: BinaryReport) -> HealthStatus:
    if not db_ok:
        return "error"
    return "ok" if report.all_ok else "degraded"


@router.get("/health", response_model=LivenessResponse)
def health(request: Request, response: Response) -> LivenessResponse:
    """Unauthenticated liveness. Deliberately says nothing about versions."""
    db_ok, db_error = database_ok(request.app.state.db_engine)
    report: BinaryReport = request.app.state.binaries

    overall = _status(db_ok, report)
    if overall == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return LivenessResponse(
        status=overall,
        app_version=__version__,
        database=DatabaseHealth(
            ok=db_ok,
            error=(
                None if db_ok else f"Database at the configured path is not reachable ({db_error})."
            ),
        ),
    )


@router.get("/health/detail", response_model=HealthResponse)
def health_detail(request: Request, response: Response, _user: CurrentUser) -> HealthResponse:
    """The full report, including binary versions. Requires authentication."""
    db_ok, db_error = database_ok(request.app.state.db_engine)
    report: BinaryReport = request.app.state.binaries

    overall = _status(db_ok, report)
    if overall == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,
        app_version=__version__,
        database=DatabaseHealth(
            ok=db_ok,
            error=(
                None if db_ok else f"Database at the configured path is not reachable ({db_error})."
            ),
        ),
        rclone=RcloneHealth(
            ok=report.rclone.ok,
            version=report.rclone.version,
            error=report.rclone.error,
            expected_version=report.expected_rclone_version,
            matches_expected=report.rclone_matches_expected,
        ),
        lftp=BinaryHealth(
            ok=report.lftp.ok,
            version=report.lftp.version,
            error=report.lftp.error,
        ),
    )
