"""Credential request and response models.

There is deliberately no field anywhere in this module that returns a secret.
SPEC section 15 makes credentials write-only through the API with no reveal
feature, so the read model carries names and kinds only. If a future change adds
a secret to a response model, that is the bug.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import CredentialKind

MAX_SECRET_LENGTH = 64 * 1024  # generous enough for a PEM private key
MIN_SECRET_LENGTH = 1


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: CredentialKind
    # Write only. For backend_secret this is a JSON object mapping rclone option
    # name to value; for other kinds it is the secret itself.
    secret: str = Field(min_length=MIN_SECRET_LENGTH, max_length=MAX_SECRET_LENGTH)
    # Only meaningful for ssh_key.
    key_passphrase: str | None = Field(default=None, max_length=MAX_SECRET_LENGTH)
    # True when the value is already in rclone's obscured form, which is the case
    # for anything copied out of an existing rclone.conf.
    is_obscured: bool = False

    @model_validator(mode="after")
    def _check_shape(self) -> CredentialCreate:
        if self.kind == CredentialKind.backend_secret:
            try:
                payload = json.loads(self.secret)
            except ValueError as exc:
                raise ValueError(
                    "A backend secret must be a JSON object mapping rclone option "
                    'names to values, for example {"secret_access_key": "..."}.'
                ) from exc
            if not isinstance(payload, dict) or not payload:
                raise ValueError(
                    "A backend secret must be a non-empty JSON object mapping "
                    "rclone option names to values."
                )
        if self.key_passphrase and self.kind != CredentialKind.ssh_key:
            raise ValueError("A key passphrase only applies to an SSH key credential.")
        return self


class CredentialUpdate(BaseModel):
    """Rename, or replace the secret. Never reveals the existing value."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    secret: str | None = Field(
        default=None, min_length=MIN_SECRET_LENGTH, max_length=MAX_SECRET_LENGTH
    )
    key_passphrase: str | None = Field(default=None, max_length=MAX_SECRET_LENGTH)
    is_obscured: bool | None = None


class CredentialRead(BaseModel):
    """Metadata only. No secret, no ciphertext, not now and not ever."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: CredentialKind
    is_obscured: bool
    created_at: datetime
    updated_at: datetime
    # Populated by the API so the UI can refuse a delete with an explanation.
    used_by: list[str] = Field(default_factory=list)
