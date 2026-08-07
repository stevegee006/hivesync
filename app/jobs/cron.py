"""Cron expression handling: validation and the next-fire preview.

Kept separate from the scheduler so the job editor can validate and preview an
expression without touching a running scheduler.

The preview is concrete fire times. SPEC section 9 suggests prose, and the
question an operator actually has is "when will this run", which timestamps
answer exactly for every expression.

`describe` was added later, for the job list, where five timestamps per card is
too much and `0 * * * *` tells a reader nothing. The original objection to a
prose renderer stands and is answered rather than ignored: it is **only** given
prose for the exact shapes the schedule builder can produce, and anything else
is returned verbatim. So it is never subtly wrong about an expression nobody
tested; it declines to describe it. The shapes are the ones in the builder's
`parse` in `jobs/form.html`, and `test_cron_description.py` asserts the two
agree.
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


# Cron day-of-week numbering, which starts at Sunday. Kept here rather than
# derived from `calendar`, whose weeks start on Monday.
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _clock_time(hour: int, minute: int, clock: str) -> str:
    if clock == "12h":
        suffix = "AM" if hour < 12 else "PM"
        shown = hour % 12 or 12
        return f"{shown}:{minute:02d} {suffix}"
    return f"{hour:02d}:{minute:02d}"


def describe(expression: str | None, *, clock: str = "24h") -> str:
    """A plain description of a schedule, or the expression itself.

    Only the shapes the schedule builder writes are described. Anything else is
    handed back unchanged, because a half-right description of a schedule that
    deletes files is worse than the expression the author typed.
    """
    raw = (expression or "").strip()
    if not raw:
        return "On demand"

    parts = raw.split()
    if len(parts) != 5:
        return raw
    minute, hour, day_of_month, month, day_of_week = parts
    # The builder only ever writes "*" for these two, so anything else is an
    # expression it did not produce and this must not guess at.
    if day_of_month != "*" or month != "*":
        return raw

    if minute.startswith("*/") and hour == "*" and day_of_week == "*":
        step = minute[2:]
        if step.isdigit() and int(step) > 0:
            return "Every minute" if int(step) == 1 else f"Every {int(step)} minutes"
        return raw

    if not minute.isdigit():
        return raw

    if hour == "*" and day_of_week == "*":
        return "Hourly, on the hour" if int(minute) == 0 else f"Hourly at {int(minute)} past"

    if not hour.isdigit():
        return raw
    if int(hour) > 23 or int(minute) > 59:
        return raw
    at = _clock_time(int(hour), int(minute), clock)

    if day_of_week == "*":
        return f"Daily at {at}"

    chosen = day_of_week.split(",")
    if not all(part.isdigit() and int(part) <= 6 for part in chosen):
        return raw
    days = [WEEKDAYS[int(part)] for part in dict.fromkeys(chosen)]
    if len(days) == 7:
        return f"Daily at {at}"
    if len(days) == 1:
        return f"{days[0]}s at {at}"
    return f"{', '.join(days[:-1])} and {days[-1]} at {at}"
