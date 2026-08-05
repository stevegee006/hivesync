"""rclone helper endpoints: backends, imported remotes, and stanza parsing.

Nothing here is a hardcoded list. SPEC section 5.2 requires the backend picker be
populated from `rclone config providers` at runtime, because a static table goes
stale the moment rclone ships a new backend or a new option.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import capabilities
from app.api.deps import CurrentUser
from app.config import Settings
from app.db import get_session
from app.engines import inspect, rcloneconf
from app.engines.rcloneconf import RemoteConfigError
from app.models import Connection
from app.schemas.connection import CapabilitySummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rclone", tags=["rclone"])


class BackendOption(BaseModel):
    name: str
    help: str
    type: str
    required: bool
    # Must be obscured before rclone will accept it.
    is_password: bool
    # Must be redacted from logs. Broader than is_password: includes host and user.
    sensitive: bool
    advanced: bool
    default: Any = None


class BackendSummary(BaseModel):
    name: str
    description: str
    options: list[BackendOption]


class ParseStanzaRequest(BaseModel):
    text: str = Field(min_length=1, max_length=64 * 1024)


class ParsedOption(BaseModel):
    key: str
    value: str
    # Secret rows are routed to an encrypted Credential rather than extra_opts.
    is_secret: bool
    # A value copied from a real rclone.conf is already obscured, and must not be
    # obscured again at run time.
    already_obscured: bool


class ParseStanzaResponse(BaseModel):
    name: str
    backend_type: str
    options: list[ParsedOption]


class OptionAvailabilityRead(BaseModel):
    available: bool
    reason: str | None = None
    warning: str | None = None


class CompatibilityResponse(BaseModel):
    """The capability intersection of two endpoints. SPEC 5.4's table."""

    source: str
    dest: str
    source_capabilities: CapabilitySummary
    dest_capabilities: CapabilitySummary
    checksum: OptionAvailabilityRead
    bidirectional: OptionAvailabilityRead
    archive: OptionAvailabilityRead
    empty_dirs: OptionAvailabilityRead
    shared_hashes: list[str]
    warnings: list[str]
    stale: bool


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/backends", response_model=list[BackendSummary])
def list_backends(
    request: Request, _user: CurrentUser, session: Session = Depends(get_session)
) -> list[BackendSummary]:
    try:
        providers = inspect.list_backends()
    except inspect.InspectError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    summaries: list[BackendSummary] = []
    for provider in providers:
        options = [
            BackendOption(
                name=str(option.get("Name", "")),
                help=str(option.get("Help", "")),
                type=str(option.get("Type", "string")),
                required=bool(option.get("Required")),
                is_password=bool(option.get("IsPassword")),
                sensitive=bool(option.get("Sensitive")),
                advanced=bool(option.get("Advanced")),
                default=option.get("Default"),
            )
            for option in provider.get("Options", [])
        ]
        summaries.append(
            BackendSummary(
                name=str(provider.get("Name", "")),
                description=str(provider.get("Description", "")),
                options=options,
            )
        )
    summaries.sort(key=lambda item: item.name)
    return summaries


@router.get("/remotes", response_model=list[str])
def list_remotes(
    request: Request, _user: CurrentUser, session: Session = Depends(get_session)
) -> list[str]:
    """Remotes from the user supplied config. Read only, never written. SPEC 5.2."""
    try:
        return inspect.list_imported_remotes(_settings(request).user_rclone_conf)
    except inspect.InspectError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/parse-stanza", response_model=ParseStanzaResponse)
def parse_stanza(
    payload: ParseStanzaRequest,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ParseStanzaResponse:
    """Turn a pasted rclone.conf section into editable rows.

    Which rows are secret comes from rclone's own IsPassword metadata for the
    backend named in the stanza, so it stays correct as backends change.
    """

    password_options: list[str] = []
    try:
        # Parse once to learn the backend, then again knowing its secret options.
        preliminary = rcloneconf.parse_stanza(payload.text)
        providers = inspect.list_backends()
        password_options = inspect.password_option_names(providers, preliminary.backend_type)
    except inspect.InspectError:
        # Without the metadata the rows are still useful, they just are not
        # classified. Better than refusing the paste outright.
        logger.warning("Could not load backend metadata while parsing a stanza")
    except RemoteConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        parsed = rcloneconf.parse_stanza(payload.text, password_options=password_options)
    except RemoteConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return ParseStanzaResponse(
        name=parsed.name,
        backend_type=parsed.backend_type,
        options=[
            ParsedOption(
                key=key,
                value=value,
                is_secret=key in parsed.secret_option_names,
                # Anything in a real rclone.conf password field is obscured.
                already_obscured=key in parsed.secret_option_names,
            )
            for key, value in parsed.options.items()
        ],
    )


@router.get("/compatibility", response_model=CompatibilityResponse)
def compatibility(
    source_id: int,
    dest_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> CompatibilityResponse:
    """What a job pairing these two endpoints could do, and why not otherwise.

    This is the logic the job editor will use to enable and disable its options.
    It is exposed now so the reason strings are exercised before that editor
    exists. See CLAUDE.md on M1 acceptance criterion four.
    """
    source = session.get(Connection, source_id)
    dest = session.get(Connection, dest_id)
    if source is None or dest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="One of those connections is gone."
        )

    result = capabilities.intersect(source, dest)

    def read(option: capabilities.OptionAvailability) -> OptionAvailabilityRead:
        return OptionAvailabilityRead(
            available=option.available, reason=option.reason, warning=option.warning
        )

    from app.api.connections import capability_summary

    return CompatibilityResponse(
        source=source.name,
        dest=dest.name,
        source_capabilities=capability_summary(source),
        dest_capabilities=capability_summary(dest),
        checksum=read(result.checksum),
        bidirectional=read(result.bidirectional),
        archive=read(result.archive),
        empty_dirs=read(result.empty_dirs),
        shared_hashes=sorted(result.shared_hashes),
        warnings=list(result.warnings),
        stale=result.stale,
    )
