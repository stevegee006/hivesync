"""Cron expression handling: validation and the next-fire preview.

Kept separate from the scheduler so the job editor can validate and preview an
expression without touching a running scheduler.

There is deliberately no prose description. SPEC section 9 suggests one, but the
question an operator actually has is "when will this run", and concrete
timestamps answer it exactly for every expression. A prose renderer is either
another dependency or bespoke string logic that is subtly wrong for the
expressions nobody tested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from app.models import utcnow

logger = logging.getLogger(__name__)

PREVIEW_COUNT = 5


class CronError(ValueError):
    """An unusable cron expression or timezone. The message is user facing."""


def build_trigger(expression: str, timezone: str) -> CronTrigger:
    """Parse a five field cron expression in a named timezone.

    Verified against APScheduler 3.11.3: `from_crontab` rejects a malformed
    expression with ValueError and an unknown zone with ZoneInfoNotFoundError,
    and it takes zoneinfo rather than pytz, so no extra dependency is involved.
    """
    cleaned = (expression or "").strip()
    if not cleaned:
        raise CronError("Enter a cron expression, or leave the schedule off entirely.")

    try:
        return CronTrigger.from_crontab(cleaned, timezone=timezone or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise CronError(
            f"'{timezone}' is not a known timezone. Use a name like "
            "America/Denver or Europe/London."
        ) from exc
    except (ValueError, TypeError) as exc:
        raise CronError(
            f"'{cleaned}' is not a valid cron expression. It needs five fields: "
            "minute, hour, day of month, month, day of week. For example "
            "'30 2 * * *' is every day at 2:30 AM."
        ) from exc


def validate(expression: str | None, timezone: str) -> None:
    """Raise CronError unless this schedule could actually run. No side effects."""
    if expression is None or not expression.strip():
        return
    build_trigger(expression, timezone)


def next_fire_times(
    expression: str, timezone: str, *, count: int = PREVIEW_COUNT, now: datetime | None = None
) -> list[datetime]:
    """The next few times this schedule would fire, in its own timezone.

    SPEC section 9's preview. Concrete timestamps rather than prose, because they
    are unambiguous for any expression and cannot drift from what the scheduler
    will actually do: this asks the same trigger object the scheduler uses.
    """
    trigger = build_trigger(expression, timezone)
    zone = ZoneInfo(timezone or "UTC")
    current = (now or utcnow()).astimezone(zone)

    fires: list[datetime] = []
    previous: datetime | None = None
    for _ in range(count):
        upcoming = trigger.get_next_fire_time(previous, current if previous is None else previous)
        if upcoming is None:
            break
        fires.append(upcoming)
        previous = upcoming
    return fires


@dataclass(frozen=True)
class SchedulePreview:
    valid: bool
    error: str | None
    fire_times: list[datetime]

    @property
    def formatted(self) -> list[str]:
        return [moment.strftime("%Y-%m-%d %H:%M:%S %Z") for moment in self.fire_times]


def preview(expression: str | None, timezone: str) -> SchedulePreview:
    """A preview that never raises, for rendering next to an input box."""
    if expression is None or not expression.strip():
        return SchedulePreview(valid=True, error=None, fire_times=[])
    try:
        return SchedulePreview(
            valid=True, error=None, fire_times=next_fire_times(expression, timezone)
        )
    except CronError as exc:
        return SchedulePreview(valid=False, error=str(exc), fire_times=[])
