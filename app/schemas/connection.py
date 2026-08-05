"""Connection request and response models.

Validation here is the first line of SPEC section 6.4's "prefer failing a job with
a clear message over guessing at intent". A connection that cannot be turned into
an rclone remote should be refused at save time, not at 2:30 AM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import ConnectionType, RcloneMode

_NEEDS_HOST = frozenset(
    {ConnectionType.sftp, ConnectionType.ftp, ConnectionType.ftps, ConnectionType.smb}
)


class ConnectionBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: ConnectionType
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    share: str | None = Field(default=None, max_length=255)
    base_path: str = ""
    username: str | None = Field(default=None, max_length=255)
    credential_id: int | None = None
    extra_opts: dict[str, Any] = Field(default_factory=dict)

    rclone_mode: RcloneMode | None = None
    rclone_remote_name: str | None = Field(default=None, max_length=128)
    rclone_backend_type: str | None = Field(default=None, max_length=64)

    # Stale mount guard for local connections. SPEC 6.4.
    sentinel_file: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _check_consistency(self) -> ConnectionBase:
        if self.type in _NEEDS_HOST and not self.host:
            raise ValueError(f"A {self.type.value} connection needs a host.")

        if self.type == ConnectionType.smb and not self.share:
            raise ValueError(
                "An SMB connection needs a share. rclone addresses SMB as "
                "remote:Share/path, so the share is the first path element."
            )

        if self.type != ConnectionType.smb and self.share:
            raise ValueError("Only an SMB connection uses a share.")

        if self.type == ConnectionType.rclone_remote:
            if self.rclone_mode is None:
                raise ValueError(
                    "An rclone remote needs a mode: inline to define it here, or "
                    "imported to use one from the mounted rclone.conf."
                )
            if self.rclone_mode == RcloneMode.inline and not self.rclone_backend_type:
                raise ValueError("An inline rclone remote needs a backend type.")
            if self.rclone_mode == RcloneMode.imported and not self.rclone_remote_name:
                raise ValueError(
                    "An imported rclone remote needs the name of a remote from the "
                    "mounted rclone.conf."
                )
        elif self.rclone_mode is not None:
            raise ValueError("A mode only applies to an rclone remote connection.")

        if self.sentinel_file and self.type != ConnectionType.local:
            raise ValueError(
                "A sentinel file only applies to a local connection. It guards "
                "against a stale network mount presenting as an empty directory."
            )

        if self.type == ConnectionType.local and not self.base_path:
            raise ValueError("A local connection needs a base path.")

        return self


class ConnectionCreate(ConnectionBase):
    pass


class ConnectionUpdate(ConnectionBase):
    """Full replacement. PATCH semantics are handled by the route merging first."""


class CapabilitySummary(BaseModel):
    """A digest of the stored probe, for display. SPEC 5.4."""

    probed: bool
    probed_at: datetime | None = None
    stale: bool
    hashes: list[str] = Field(default_factory=list)
    can_set_modtime: bool = False
    supports_move: bool = False
    supports_empty_dirs: bool = False
    case_insensitive: bool = False
    bucket_based: bool = False


class ConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: ConnectionType
    host: str | None
    port: int | None
    share: str | None
    base_path: str
    username: str | None
    credential_id: int | None
    extra_opts: dict[str, Any]
    rclone_mode: RcloneMode | None
    rclone_remote_name: str | None
    rclone_backend_type: str | None
    sentinel_file: str | None
    # Public keys. Safe to show, and showing them is the point: SPEC 15 requires
    # the fingerprint be visible for review.
    host_keys: str | None
    host_keys_trusted: bool
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_test_error: str | None
    created_at: datetime
    updated_at: datetime
    capabilities: CapabilitySummary | None = None
    # The rclone path this resolves to, for example synology:Media/photos.
    # SPEC 13 requires the resolved form be visible so there is no ambiguity.
    resolved_path: str | None = None


class HostKeyPrompt(BaseModel):
    """Untrusted host keys awaiting explicit approval. SPEC 15, trust on first use."""

    fingerprint: str
    fingerprints: list[str] = Field(default_factory=list)
    prompt: str


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    capabilities: CapabilitySummary | None = None
    # Present when an SFTP host key needs trusting before the test can pass.
    host_key: HostKeyPrompt | None = None
    warnings: list[str] = Field(default_factory=list)
    # Redacted, so it is safe to show. SPEC 6.1.
    command: str | None = None


class TrustHostKeyRequest(BaseModel):
    """Explicit approval of a specific fingerprint.

    The fingerprint is required rather than implied, so approving cannot race with
    a key that changed between the prompt and the click.
    """

    fingerprint: str = Field(min_length=1, max_length=255)


class BrowseEntry(BaseModel):
    name: str
    is_dir: bool
    path: str


class BrowseResult(BaseModel):
    path: str
    parent: str | None
    entries: list[BrowseEntry]
