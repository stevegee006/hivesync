"""A configured endpoint: one protocol, one location, optionally one credential.

Two columns here are not in SPEC section 4's model but are required by features
the spec mandates elsewhere, so they are in the baseline rather than a later
migration:

- sentinel_file, required by SPEC section 6.4. A stale cifs or NFS mount presents
  as an empty directory, which is exactly the failure mode that causes a mass
  delete. The check needs somewhere to store the filename.
- host_keys, required by SPEC section 15. Trust on first use pins the server's
  keys per connection, and the run fails if they change.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UtcDateTime, str_enum
from app.models.credential import Credential


class ConnectionType(enum.StrEnum):
    local = "local"
    sftp = "sftp"
    ftp = "ftp"
    ftps = "ftps"
    smb = "smb"
    rclone_remote = "rclone_remote"


class RcloneMode(enum.StrEnum):
    """How an rclone_remote connection is defined. SPEC section 5.2."""

    inline = "inline"
    imported = "imported"


class Connection(Base, TimestampMixin):
    __tablename__ = "connection"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    type: Mapped[ConnectionType] = mapped_column(str_enum(ConnectionType), nullable=False)

    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SMB only. rclone paths are remote:Share/sub/path, so the first path element
    # is the share and has to be captured separately. SPEC section 5.1.
    share: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_path: Mapped[str] = mapped_column(Text, nullable=False, default="")

    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # RESTRICT, not CASCADE. Deleting a credential that a connection depends on
    # must fail loudly rather than quietly breaking every job that uses it.
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credential.id", ondelete="RESTRICT"), nullable=True
    )
    credential: Mapped[Credential | None] = relationship(lazy="joined")

    extra_opts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # rclone_remote only.
    rclone_mode: Mapped[RcloneMode | None] = mapped_column(
        str_enum(RcloneMode, length=16), nullable=True
    )
    rclone_remote_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rclone_backend_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Populated by the capability probe. SPEC section 5.4. Shape is whatever the
    # pinned rclone reports, deliberately not modelled as columns.
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    capabilities_probed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Stale mount guard for local connections. SPEC section 6.4.
    sentinel_file: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Pinned SSH host keys, one known_hosts style "type base64" entry per line.
    # SPEC section 15.
    #
    # Every key the server offers is stored, not just one. ssh-keyscan returns
    # several (commonly ssh-rsa and ssh-ed25519) and the client negotiates one of
    # them; pinning a single key silently forces that algorithm and breaks the day
    # the server stops offering it. Text, not String(255), because a single RSA
    # entry is already longer than that.
    host_keys: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Whether those keys have been approved by a human. They are recorded at scan
    # time and only honoured once this is true.
    #
    # Splitting "scanned" from "trusted" exists to avoid a second ssh-keyscan at
    # approval time. OpenSSH 9.8 added PerSourcePenalties, which penalises sources
    # that connect without authenticating, and ssh-keyscan does exactly that. Two
    # scans per approval gets an operator's own address throttled within a few
    # clicks, and then host key approval starts failing for no visible reason.
    host_keys_trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_test_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"Connection(id={self.id!r}, name={self.name!r}, type={self.type!r})"
