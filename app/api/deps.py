"""Shared FastAPI dependencies.

Authentication is a dependency rather than a call inside each handler so that it
runs during dependency resolution, before the request body is validated. An
unauthenticated caller then gets a flat 401 instead of a 422 describing the shape
of a payload they were never entitled to submit.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app import security
from app.config import Settings
from app.crypto import SecretBox
from app.db import get_session
from app.models import User


def _require_user(request: Request, session: Session = Depends(get_session)) -> User:
    return security.require_user(request, session)


def _secret_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secrets
    return box


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


CurrentUser = Annotated[User, Depends(_require_user)]
Secrets = Annotated[SecretBox, Depends(_secret_box)]
AppSettings = Annotated[Settings, Depends(_settings)]
DbSession = Annotated[Session, Depends(get_session)]
