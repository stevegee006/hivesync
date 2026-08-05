"""API routers, mirroring SPEC.md section 12.

M0 shipped auth and health. M1 adds connections, credentials and the rclone
helpers. Jobs, runs, settings and metrics arrive with their own milestones.
"""

from fastapi import APIRouter

from app.api import auth, connections, credentials, health, jobs, rclone

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(credentials.router)
api_router.include_router(connections.router)
api_router.include_router(rclone.router)
api_router.include_router(jobs.router)

__all__ = ["api_router"]
