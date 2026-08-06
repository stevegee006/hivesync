"""The Prometheus endpoint. SPEC section 16.

Mounted at `/metrics`, outside the `/api` prefix, because that is where every
scrape config looks for it.

**Not open.** The labels carry job names, which in this application are the names
people give to their shares and directories. Two ways in:

1. A logged-in session, so the Settings screen can link to it.
2. `Authorization: Bearer <HIVESYNC_METRICS_TOKEN>`, for Prometheus.

A wrong or missing token gets 401 with a WWW-Authenticate header, not a redirect
to the login page: a scraper cannot follow one, and an HTML login form parsed as
an exposition produces confusing failures rather than clear ones.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from sqlalchemy.orm import Session

from app import metrics, security
from app.config import Settings

router = APIRouter()


def _token_ok(request: Request, settings: Settings) -> bool:
    expected = (settings.metrics_token or "").strip()
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    # Constant time: this is a bearer token on an unauthenticated path.
    return hmac.compare_digest(presented.strip(), expected)


@router.get("/metrics")
def prometheus_metrics(request: Request) -> Response:
    settings: Settings = request.app.state.settings

    if not _token_ok(request, settings):
        session_factory = request.app.state.session_factory
        session: Session = session_factory()
        try:
            security.require_user(request, session)
        except security.NotAuthenticated:
            return Response(
                content="Authentication required.\n",
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="hivesync"'},
                media_type="text/plain; charset=utf-8",
            )
        finally:
            session.close()

    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        body = metrics.render(session)
    finally:
        session.close()
    return Response(content=body, media_type=metrics.CONTENT_TYPE)
