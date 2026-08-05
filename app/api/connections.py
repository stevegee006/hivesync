"""Connection endpoints: CRUD, test, browse, and host key trust.

SPEC section 12 defines the shape. The test endpoint does double duty: it proves
reachability and it populates the capability probe that the job editor later
derives its constraints from.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import capabilities, probe
from app.api.deps import CurrentUser
from app.config import Settings
from app.crypto import SecretBox
from app.db import get_session
from app.engines import inspect
from app.engines.rcloneconf import RemoteConfigError, join_subpath
from app.models import Connection, Job
from app.schemas.connection import (
    BrowseEntry,
    BrowseResult,
    CapabilitySummary,
    ConnectionCreate,
    ConnectionRead,
    ConnectionTestResult,
    ConnectionUpdate,
    HostKeyPrompt,
    TrustHostKeyRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])

_EDITABLE_FIELDS = (
    "name",
    "type",
    "host",
    "port",
    "share",
    "base_path",
    "username",
    "credential_id",
    "extra_opts",
    "rclone_mode",
    "rclone_remote_name",
    "rclone_backend_type",
    "sentinel_file",
)


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secrets
    return box


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def capability_summary(connection: Connection) -> CapabilitySummary:
    caps = capabilities.for_connection(connection)
    return CapabilitySummary(
        probed=caps.probed,
        probed_at=caps.probed_at,
        stale=caps.is_stale,
        hashes=sorted(caps.hashes),
        can_set_modtime=caps.can_set_modtime,
        supports_move=caps.supports_move,
        supports_empty_dirs=caps.supports_empty_dirs,
        case_insensitive=caps.case_insensitive,
        bucket_based=caps.is_bucket_based,
    )


def to_read(connection: Connection, request: Request | None = None) -> ConnectionRead:
    """Build the read model explicitly.

    model_validate cannot be used here: the schema's `capabilities` field is a
    digest, while the ORM attribute of the same name holds the raw probe payload.
    from_attributes would feed the latter into the former, which only fails once a
    connection has actually been probed, so the naive version passes every test
    written before the first successful test.
    """
    fields = {
        name: getattr(connection, name)
        for name in ConnectionRead.model_fields
        if name not in {"capabilities", "resolved_path"}
    }
    model = ConnectionRead(**fields, capabilities=capability_summary(connection))
    if request is not None:
        model.resolved_path = probe.resolved_path(
            connection, box=_box(request), settings=_settings(request)
        )
    return model


def _get_or_404(session: Session, connection_id: int) -> Connection:
    connection = session.get(Connection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such connection.")
    return connection


def _apply(connection: Connection, payload: ConnectionCreate | ConnectionUpdate) -> None:
    for name in _EDITABLE_FIELDS:
        setattr(connection, name, getattr(payload, name))


@router.get("", response_model=list[ConnectionRead])
def list_connections(
    request: Request, _user: CurrentUser, session: Session = Depends(get_session)
) -> list[ConnectionRead]:
    connections = session.scalars(select(Connection).order_by(Connection.name)).all()
    return [to_read(connection) for connection in connections]


@router.post("", response_model=ConnectionRead, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: ConnectionCreate,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ConnectionRead:
    connection = Connection()
    _apply(connection, payload)
    session.add(connection)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A connection named '{payload.name}' already exists, or the "
                "selected credential does not exist."
            ),
        ) from exc
    logger.info("Created connection", extra={"connection": connection.name})
    return to_read(connection, request)


@router.get("/{connection_id}", response_model=ConnectionRead)
def get_connection(
    connection_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ConnectionRead:
    return to_read(_get_or_404(session, connection_id), request)


@router.patch("/{connection_id}", response_model=ConnectionRead)
def update_connection(
    connection_id: int,
    payload: ConnectionUpdate,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ConnectionRead:
    connection = _get_or_404(session, connection_id)

    # Changing what the connection points at invalidates the probe, and a stale
    # probe silently permitting a job option is worse than an unprobed one
    # blocking it. SPEC 5.4 says re-probe on edit.
    identity_before = (connection.type, connection.host, connection.port, connection.share)
    _apply(connection, payload)
    identity_after = (connection.type, connection.host, connection.port, connection.share)
    if identity_before != identity_after:
        connection.capabilities = None
        connection.capabilities_probed_at = None
        connection.last_test_ok = None
        connection.last_test_error = None

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another connection already has that name, or the credential is gone.",
        ) from exc
    return to_read(connection, request)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> None:
    connection = _get_or_404(session, connection_id)

    users = list(
        session.scalars(
            select(Job.name).where(
                (Job.source_connection_id == connection_id)
                | (Job.dest_connection_id == connection_id)
            )
        )
    )
    if users:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That connection is used by {', '.join(sorted(users))}. Delete or "
                "repoint those jobs first."
            ),
        )

    session.delete(connection)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That connection is still in use."
        ) from exc


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(
    connection_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ConnectionTestResult:
    connection = _get_or_404(session, connection_id)

    outcome = probe.test_connection(
        connection, box=_box(request), settings=_settings(request), session=session
    )

    return ConnectionTestResult(
        ok=outcome.ok,
        message=outcome.message,
        capabilities=capability_summary(connection),
        host_key=(
            HostKeyPrompt(
                fingerprint=outcome.host_key.fingerprint,
                fingerprints=outcome.host_key.fingerprints,
                prompt=outcome.host_key.prompt,
            )
            if outcome.host_key
            else None
        ),
        warnings=outcome.warnings,
        command=outcome.command,
    )


@router.post("/{connection_id}/trust-host-key", response_model=ConnectionRead)
def trust_host_key(
    connection_id: int,
    payload: TrustHostKeyRequest,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ConnectionRead:
    """Pin an SSH host key after the operator has reviewed the fingerprint.

    SPEC 15 requires this be explicit. The fingerprint is re-checked so approving
    cannot pin a key other than the one that was displayed.
    """
    connection = _get_or_404(session, connection_id)
    try:
        probe.trust_host_key(connection, payload.fingerprint, session=session)
    except RemoteConfigError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return to_read(connection, request)


@router.delete("/{connection_id}/host-key", response_model=ConnectionRead)
def forget_host_key(
    connection_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> ConnectionRead:
    """Unpin a host key, for a legitimately rebuilt server."""
    connection = _get_or_404(session, connection_id)
    connection.host_keys = None
    connection.host_keys_trusted = False
    session.commit()
    logger.warning("Cleared pinned SSH host key", extra={"connection": connection.name})
    return to_read(connection, request)


@router.get("/{connection_id}/browse", response_model=BrowseResult)
def browse_connection(
    connection_id: int,
    request: Request,
    _user: CurrentUser,
    path: str | None = None,
    session: Session = Depends(get_session),
) -> BrowseResult:
    connection = _get_or_404(session, connection_id)

    try:
        entries = probe.browse(connection, path, box=_box(request), settings=_settings(request))
    except RemoteConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except inspect.InspectError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    current = (path or "").strip("/")
    parent = None
    if current:
        parts = current.split("/")
        parent = "/".join(parts[:-1])

    return BrowseResult(
        path=current,
        parent=parent,
        entries=[
            BrowseEntry(
                name=entry.name,
                is_dir=entry.is_dir,
                path=join_subpath(current, entry.name),
            )
            for entry in entries
        ],
    )
