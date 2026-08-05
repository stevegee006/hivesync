"""Stored credential material.

Ciphertext only. Nothing in this table is ever serialized back to a client:
SPEC section 15 makes credentials write-only through the API, with no reveal
feature. The schemas layer has no read model for these columns by design.

Encryption and decryption happen exclusively in app.crypto.
"""

from __future__ import annotations

import enum

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum


class CredentialKind(enum.StrEnum):
    password = "password"
    ssh_key = "ssh_key"
    smb_ntlm = "smb_ntlm"
    backend_secret = "backend_secret"


class Credential(Base, TimestampMixin):
    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    kind: Mapped[CredentialKind] = mapped_column(str_enum(CredentialKind), nullable=False)

    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Passphrase for an encrypted SSH private key, when the kind is ssh_key.
    key_passphrase_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    def __repr__(self) -> str:
        # No ciphertext in the repr. It is not secret material in the plaintext
        # sense, but it has no business in a log line either.
        return f"Credential(id={self.id!r}, name={self.name!r}, kind={self.kind!r})"
