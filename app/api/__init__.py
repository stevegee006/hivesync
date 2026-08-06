"""API routers, mirroring SPEC.md section 12.

M0 shipped auth and health. M1 adds connections, credentials and the rclone
helpers. Jobs and runs arrive with M2 to M5, settings and presets with M7.

`metrics.router` is deliberately not included here: it is mounted at the
application root by main.create_app, because /metrics is where a scrape config
looks and /api/metrics is not.
"""

from fastapi import APIRouter

from app.api import auth, connections, credentials, health, jobs, presets, rclone, settings

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(credentials.router)
api_router.include_router(connections.router)
api_router.include_router(rclone.router)
api_router.include_router(jobs.router)
api_router.include_router(presets.router)
api_router.include_router(settings.router)

__all__ = ["api_router"]
