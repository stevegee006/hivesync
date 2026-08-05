"""Server rendered pages. Jinja2 templates, no build step.

M0 ships the login page, the forced password change page, and a dashboard shell.
The dashboard is deliberately an empty state: job cards arrive with the jobs
milestone rather than being faked here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import __version__, security
from app.api.auth import safe_redirect_target
from app.binaries import BinaryReport
from app.db import get_session

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter(include_in_schema=False)

# Error codes are fixed strings mapped to copy here, so nothing from a query
# string is ever rendered into the page.
_LOGIN_ERRORS = {
    "invalid": "That username and password combination is not valid.",
    "missing": "Enter both a username and a password.",
}

_PASSWORD_ERRORS = {
    "missing": "Enter your current password and a new one.",
    "wrong": "The current password is not correct.",
    "weak": f"The new password must be at least {security.MIN_PASSWORD_LENGTH} characters long.",
    "same": "The new password must differ from your current one.",
}


def _base_context(request: Request) -> dict[str, Any]:
    report: BinaryReport = request.app.state.binaries
    return {
        "request": request,
        "app_version": __version__,
        "auth_mode": request.app.state.settings.auth_mode,
        "rclone_version": report.rclone.version,
        "lftp_version": report.lftp.version,
        "min_password_length": security.MIN_PASSWORD_LENGTH,
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
    session: Session = Depends(get_session),
) -> Any:
    if security.current_user(request, session) is not None:
        return RedirectResponse(url="/", status_code=303)
    context = _base_context(request)
    context["error"] = _LOGIN_ERRORS.get(error or "")
    context["next"] = safe_redirect_target(next)
    return templates.TemplateResponse(request, "login.html", context)


@router.get("/account/password", response_class=HTMLResponse)
def password_page(
    request: Request,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> Any:
    user = security.require_user(request, session)
    context = _base_context(request)
    context["user"] = user
    context["error"] = _PASSWORD_ERRORS.get(error or "")
    context["forced"] = user.must_change_password
    return templates.TemplateResponse(request, "account_password.html", context)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> Any:
    user = security.require_user(request, session)
    if user.must_change_password:
        return RedirectResponse(url="/account/password", status_code=303)
    context = _base_context(request)
    context["user"] = user
    return templates.TemplateResponse(request, "dashboard.html", context)
