"""Liveness and version reporting.

Unauthenticated, because the container HEALTHCHECK calls it. That means the
pinned binary versions are readable without a session, which is a small
information disclosure noted for M8 hardening: the fix is to split bare liveness
from version detail. It stays as-is for now because M0's acceptance criterion is
that health reports the rclone and lftp versions.

Status semantics:
- database unreachable, 503. The app cannot function and the container should be
  restarted.
- a missing or unexpected binary, 200 with status "degraded". The UI stays
  reachable so the operator can see what is wrong, which a restart loop prevents.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app import __version__
from app.binaries import BinaryReport
from app.db import database_ok
from app.schemas.health import (
    BinaryHealth,
    DatabaseHealth,
    HealthResponse,
    HealthStatus,
    RcloneHealth,
)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request, response: Response) -> HealthResponse:
    db_ok, db_error = database_ok(request.app.state.db_engine)
    report: BinaryReport = request.app.state.binaries

    overall: HealthStatus
    if not db_ok:
        overall = "error"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not report.all_ok:
        overall = "degraded"
    else:
        overall = "ok"

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
