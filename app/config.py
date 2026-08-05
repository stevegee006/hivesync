"""Application configuration, sourced entirely from the environment.

This module deliberately does not import app.crypto. It carries the raw secret
key as an opaque string and never inspects it; validation of the key belongs to
the crypto module, which is the only place allowed to reason about key material.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["local", "trusted_header", "none"]


class Settings(BaseSettings):
    """Environment-driven settings. See SPEC.md section 14."""

    model_config = SettingsConfigDict(
        env_prefix="HIVESYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required. Refuse to start without it, see main.main().
    secret_key: str = ""

    # Bootstrap admin. The password is used once, on an empty user table.
    admin_user: str = "admin"
    admin_password: str | None = None

    auth_mode: AuthMode = "local"
    trusted_header: str | None = None
    # Comma separated CIDRs. Read through the trusted_proxy_list property.
    trusted_proxies: str = ""

    log_level: str = "info"

    # Volume layout. Overridable so the app can run outside a container.
    config_dir: Path = Path("/config")

    host: str = "0.0.0.0"  # noqa: S104 - binding all interfaces is the point of a container
    port: int = 8080

    # Set by the Dockerfile to the rclone version it installed, so /api/health
    # can report a mismatch between the intended and the actual binary.
    expected_rclone_version: str | None = None

    # Homelab deployments are commonly plain HTTP behind a reverse proxy.
    session_https_only: bool = False
    session_max_age_seconds: int = 60 * 60 * 12

    timezone: str = Field(default="UTC", validation_alias=AliasChoices("TZ"))

    @property
    def db_path(self) -> Path:
        return self.config_dir / "hivesync.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def bisync_dir(self) -> Path:
        return self.config_dir / "bisync"

    @property
    def user_rclone_conf(self) -> Path:
        """User-supplied rclone config. Read only, never written. SPEC 5.2."""
        return self.config_dir / "rclone" / "rclone.conf"

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]

    @model_validator(mode="after")
    def _check_auth_mode(self) -> Settings:
        if self.auth_mode == "trusted_header":
            if not self.trusted_header:
                raise ValueError(
                    "HIVESYNC_AUTH_MODE is trusted_header but HIVESYNC_TRUSTED_HEADER "
                    "is not set. Set it to the header your proxy injects, for example "
                    "X-Authentik-Username."
                )
            if not self.trusted_proxy_list:
                raise ValueError(
                    "HIVESYNC_AUTH_MODE is trusted_header but HIVESYNC_TRUSTED_PROXIES "
                    "is empty. Without a CIDR allowlist any client could forge the "
                    "identity header. Set it to the address range of your proxy."
                )
        return self

    def ensure_directories(self) -> None:
        """Create the writable directories the app owns. Never touches /data."""
        for path in (self.config_dir, self.log_dir, self.bisync_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
