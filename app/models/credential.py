"""Stored credential material.

Ciphertext only. Nothing in this table is ever serialized back to a client:
SPEC section 15 makes credentials write-only through the API, with no reveal
feature. The schemas layer has no read model for these columns by design.

Encryption and decryption happen exclusively in app.crypto.

How secret_ciphertext is interpreted, by kind:

- password, smb_ntlm: the plaintext secret, a single string.
- ssh_key: the private key in PEM form. Its passphrase, if any, lives in
  key_passphrase_ciphertext.
- backend_secret: a JSON object mapping rclone option name to
  {"value": str, "obscured": bool}. An inline rclone_remote can need several
  secrets, for example an access key and a secret key, and Connection carries
  only one credential_id, so they travel together in one encrypted blob.

is_obscured records whether the stored value is already in rclone's obscured
form. Values pasted from an existing rclone.conf arrive obscured, and
`rclone reveal` accepts a value only as an argument, never on stdin, so
normalising them back to plaintext would put a reversible secret in an argv
visible through /proc. Tracking the state is cheaper than that exposure.
"""

from __future__ import annotations

import enum

from sqlalchemy import Boolean, LargeBinary, String
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

    # True when the stored value is already in rclone's obscured form, which is
    # the case for anything imported from an existing rclone.conf stanza.
    is_obscured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        # No ciphertext in the repr. It is not secret material in the plaintext
        # sense, but it has no business in a log line either.
        return f"Credential(id={self.id!r}, name={self.name!r}, kind={self.kind!r})"
