"""Application preferences, stored in the `setting` table.

Separate from `app.config.Settings`, which is environment only and read at boot.
These are the values an operator changes from the Settings screen while the
application is running: notification targets, retention, and defaults for new
jobs.

One row per field rather than a single JSON blob. A key/value table is what SPEC
section 4 specifies, and it means a value can be corrected with one UPDATE
against a running instance, which matters when the thing that is wrong is the
notification target that would have told you something is wrong.

**No secret is ever stored here.** A webhook URL can carry a token in its path,
which is why `Preferences.redacted()` exists and why the export in
`app/portable.py` drops the URL rather than shipping it.
"""

from __future__ import annotations

import logging
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

logger = logging.getLogger(__name__)

NotifyTarget = Literal["none", "webhook", "ntfy", "discord"]
Clock = Literal["24h", "12h"]

# Values that are not preferences and must not be served or overwritten by the
# Settings screen. The key fingerprint lives in the same table.
RESERVED_KEYS = frozenset({"secret_key_fingerprint"})

PREFIX = "pref."


class Preferences(BaseModel):
    """Everything the Settings screen can change.

    Defaults here are the defaults a fresh install runs with, so changing one
    changes behaviour for anyone who never opened the screen. That is intended
    for retention, which is off by default, and it is why notifications are too.
    """

    # ----------------------------------------------------------- notifications
    notify_target: NotifyTarget = "none"
    # A webhook URL frequently carries its own token, so treat it as sensitive
    # even though it is not a credential.
    notify_webhook_url: str = ""
    notify_ntfy_server: str = "https://ntfy.sh"
    notify_ntfy_topic: str = ""
    # Bounded so a hung notification endpoint cannot pin a worker thread.
    notify_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # Used to build the deep link in a notification payload. Without it the
    # notification can still say what happened, just not link to it.
    base_url: str = ""

    # ---------------------------------------------------------------- retention
    # Archive pruning is off unless a number is set, on both counts: the global
    # default and the per-job override. Deleting from the archive is the one
    # operation here with nothing behind it.
    archive_retention_days: int | None = Field(default=None, ge=1)
    # Run rows and their per-run log files.
    run_history_keep: int = Field(default=200, ge=10, le=10000)
    log_retention_days: int = Field(default=90, ge=1, le=3650)
    log_max_total_mb: int = Field(default=512, ge=16, le=102400)

    # ------------------------------------------------------------- presentation
    # How timestamps are rendered. Storage is always UTC; this only decides what
    # the screen says.
    #
    # Empty means follow the TZ environment variable, which is what a container
    # operator sets and what the scheduler already logs at startup. A value here
    # overrides it for the UI without a restart, which is the point: TZ is baked
    # into the compose file and changing it means recreating the container.
    display_timezone: str = ""
    clock: Clock = "24h"

    @field_validator("display_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        """Refuse a zone the machine cannot resolve.

        Stored unchecked, every timestamp on every page would silently fall back
        to UTC, and the only symptom would be times that are quietly wrong.
        """
        zone = value.strip()
        if not zone:
            return ""
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{zone!r} is not a timezone this machine knows. Use a name from the "
                "IANA database, for example America/Denver or Europe/London."
            ) from exc
        return zone

    def redacted(self) -> dict[str, object]:
        """A form safe to log or export. See the module docstring."""
        data = self.model_dump()
        if self.notify_webhook_url:
            data["notify_webhook_url"] = "***"
        return data


def load(session: Session) -> Preferences:
    """Read preferences, falling back to defaults for anything unset or invalid.

    A value that fails validation is dropped with a log line rather than raising.
    A single bad row, whatever wrote it, must not take down the scheduler and the
    settings screen that would let someone fix it.
    """
    stored: dict[str, str] = {
        row.key[len(PREFIX) :]: row.value or ""
        for row in session.scalars(select(Setting).where(Setting.key.startswith(PREFIX)))
    }
    if not stored:
        return Preferences()
    try:
        return Preferences.model_validate(_coerce(stored))
    except ValidationError as exc:
        logger.error("Stored preferences are invalid, using defaults", extra={"error": str(exc)})
        return Preferences()


def _coerce(stored: dict[str, str]) -> dict[str, object]:
    """Text rows into the shapes Pydantic expects.

    Everything arrives as text from a TEXT column, so an empty string has to mean
    "unset" for the optional integers or a fresh install would fail validation on
    its own defaults.
    """
    fields = Preferences.model_fields
    coerced: dict[str, object] = {}
    for key, value in stored.items():
        if key not in fields:
            # A preference removed in a later version. Ignore it rather than
            # failing; the row is harmless and deleting it is not our call.
            continue
        coerced[key] = None if value == "" and key.endswith("_days") else value
    return coerced


def save(session: Session, preferences: Preferences) -> Preferences:
    """Write every preference, replacing what is there.

    Whole-object writes rather than a patch: the Settings form submits every
    field, and a partial write is how a form ends up disagreeing with the
    database about what is configured.
    """
    for key, value in preferences.model_dump().items():
        row_key = f"{PREFIX}{key}"
        row = session.get(Setting, row_key)
        text = "" if value is None else str(value)
        if row is None:
            session.add(Setting(key=row_key, value=text))
        else:
            row.value = text
    session.commit()
    logger.info("Preferences updated", extra={"preferences": preferences.redacted()})
    return preferences
