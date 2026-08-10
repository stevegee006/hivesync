"""Response models for /api/health."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

HealthStatus = Literal["ok", "degraded", "error"]


class DatabaseHealth(BaseModel):
    ok: bool
    error: str | None = None


class BinaryHealth(BaseModel):
    ok: bool
    version: str | None = None
    error: str | None = None


class RcloneHealth(BinaryHealth):
    expected_version: str | None = None
    # None when the image declared no expectation. False means the installed
    # binary is not the pinned build that was reviewed for this release.
    matches_expected: bool | None = None


class LivenessResponse(BaseModel):
    """The unauthenticated answer. No binary versions: see app/api/health.py."""

    status: HealthStatus
    app_version: str
    database: DatabaseHealth


class HealthResponse(LivenessResponse):
    """The authenticated answer, with the version detail."""

    rclone: RcloneHealth
