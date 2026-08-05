"""Credential endpoints. Write only, per SPEC section 15.

Every route here returns CredentialRead, which has no secret field. There is no
GET that returns a secret, and no reveal endpoint. Encryption happens in
app.crypto and nowhere else.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.crypto import SecretBox
from app.db import get_session
from app.models import Connection, Credential
from app.schemas.credential import CredentialCreate, CredentialRead, CredentialUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credentials", tags=["credentials"])


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secrets
    return box


def _users_of(session: Session, credential_id: int) -> list[str]:
    return list(
        session.scalars(select(Connection.name).where(Connection.credential_id == credential_id))
    )


def _to_read(session: Session, credential: Credential) -> CredentialRead:
    model = CredentialRead.model_validate(credential)
    model.used_by = _users_of(session, credential.id)
    return model


@router.get("", response_model=list[CredentialRead])
def list_credentials(
    request: Request, _user: CurrentUser, session: Session = Depends(get_session)
) -> list[CredentialRead]:
    credentials = session.scalars(select(Credential).order_by(Credential.name)).all()
    return [_to_read(session, credential) for credential in credentials]


@router.post("", response_model=CredentialRead, status_code=status.HTTP_201_CREATED)
def create_credential(
    payload: CredentialCreate,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> CredentialRead:
    box = _box(request)

    credential = Credential(
        name=payload.name,
        kind=payload.kind,
        secret_ciphertext=box.encrypt(payload.secret),
        key_passphrase_ciphertext=(
            box.encrypt(payload.key_passphrase) if payload.key_passphrase else None
        ),
        is_obscured=payload.is_obscured,
    )
    session.add(credential)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A credential named '{payload.name}' already exists.",
        ) from exc

    # Name and kind only. Never the secret, not even in a debug log.
    logger.info(
        "Created credential", extra={"credential": credential.name, "kind": credential.kind.value}
    )
    return _to_read(session, credential)


@router.patch("/{credential_id}", response_model=CredentialRead)
def update_credential(
    credential_id: int,
    payload: CredentialUpdate,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> CredentialRead:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such credential.")

    box = _box(request)
    if payload.name is not None:
        credential.name = payload.name
    if payload.secret is not None:
        credential.secret_ciphertext = box.encrypt(payload.secret)
    if payload.key_passphrase is not None:
        credential.key_passphrase_ciphertext = (
            box.encrypt(payload.key_passphrase) if payload.key_passphrase else None
        )
    if payload.is_obscured is not None:
        credential.is_obscured = payload.is_obscured

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another credential already has that name.",
        ) from exc

    logger.info("Updated credential", extra={"credential": credential.name})
    return _to_read(session, credential)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    credential_id: int,
    request: Request,
    _user: CurrentUser,
    session: Session = Depends(get_session),
) -> None:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such credential.")

    # Checked here for a readable message. The database enforces it regardless,
    # via ON DELETE RESTRICT, which is what protects every other code path.
    users = _users_of(session, credential_id)
    if users:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That credential is still used by "
                f"{', '.join(sorted(users))}. Point those connections at another "
                "credential first."
            ),
        )

    session.delete(credential)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That credential is still in use.",
        ) from exc
    logger.info("Deleted credential", extra={"credential_id": credential_id})
