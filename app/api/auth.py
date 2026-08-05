"""Login, logout and password change.

One endpoint serves both the HTML form and API clients: the response shape is
chosen from the request content type, so the login page needs no JavaScript and
still works if the vendored assets are missing.

No CSRF token yet. M8 owns that, per SPEC section 18.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import security
from app.db import get_session
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_FORM_TYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data"})

# Starlette renamed its 422 constant. Use the literal so this module does not
# depend on which spelling the installed version exposes.
HTTP_422_UNPROCESSABLE = 422


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


def wants_html(request: Request) -> bool:
    """Whether to answer with a redirect rather than JSON.

    A form submission is the strong signal. The Accept header is a fallback,
    which matters for a posted form that carries no body at all, such as the
    sign out button, where some clients send no content type.
    """
    if _content_type(request) in _FORM_TYPES:
        return True
    return "text/html" in request.headers.get("accept", "")


async def _payload(request: Request) -> dict[str, Any]:
    if _content_type(request) in _FORM_TYPES:
        form = await request.form()
        return {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be JSON or a form submission.",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object.",
        )
    return body


def safe_redirect_target(candidate: str | None) -> str:
    """Constrain a post-login redirect to this application.

    Rejects absolute URLs, protocol relative URLs and anything that is not a
    plain path, so the login form cannot be used as an open redirect.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


@router.post("/login")
async def login(request: Request, session: Session = Depends(get_session)) -> Any:
    html = wants_html(request)
    data = await _payload(request)
    target = safe_redirect_target(data.get("next"))

    try:
        credentials = LoginRequest.model_validate(data)
    except ValidationError as exc:
        if html:
            return RedirectResponse(
                url="/login?error=missing", status_code=status.HTTP_303_SEE_OTHER
            )
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail="A username and password are required.",
        ) from exc

    user = security.authenticate(session, credentials.username, credentials.password)
    if user is None:
        # Deliberately identical for an unknown user and a wrong password.
        logger.warning("Failed login attempt", extra={"username": credentials.username})
        if html:
            return RedirectResponse(
                url="/login?error=invalid", status_code=status.HTTP_303_SEE_OTHER
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That username and password combination is not valid.",
        )

    security.start_session(request, user)
    logger.info("Login succeeded", extra={"username": user.username})

    if html:
        destination = "/account/password" if user.must_change_password else target
        return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)
    return LoginResponse(username=user.username, must_change_password=user.must_change_password)


@router.post("/logout")
async def logout(request: Request) -> Any:
    security.end_session(request)
    if wants_html(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse({"detail": "Signed out."})


@router.post("/change-password")
async def change_password(request: Request, session: Session = Depends(get_session)) -> Any:
    html = wants_html(request)
    user = security.require_user(request, session)
    data = await _payload(request)

    def fail(code: str, message: str, http_status: int) -> Any:
        if html:
            return RedirectResponse(
                url=f"/account/password?error={code}", status_code=status.HTTP_303_SEE_OTHER
            )
        raise HTTPException(status_code=http_status, detail=message)

    try:
        payload = ChangePasswordRequest.model_validate(data)
    except ValidationError:
        return fail(
            "missing",
            "The current and new password are both required.",
            HTTP_422_UNPROCESSABLE,
        )

    ok, _ = security.verify_password(user.password_hash, payload.current_password)
    if not ok:
        return fail("wrong", "The current password is not correct.", status.HTTP_403_FORBIDDEN)

    weakness = security.validate_password_strength(payload.new_password)
    if weakness:
        return fail("weak", weakness, HTTP_422_UNPROCESSABLE)

    if payload.new_password == payload.current_password:
        return fail(
            "same",
            "The new password must differ from the current one.",
            HTTP_422_UNPROCESSABLE,
        )

    security.set_password(session, user, payload.new_password)
    logger.info("Password changed", extra={"username": user.username})

    if html:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return MessageResponse(detail="Password updated.")
