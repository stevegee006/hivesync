"""API routers, mirroring SPEC.md section 12.

Only the auth and health routes exist at M0. Connections, credentials, jobs,
runs, rclone helpers, settings and metrics arrive with their own milestones.
"""

from fastapi import APIRouter

from app.api import auth, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)

__all__ = ["api_router"]
