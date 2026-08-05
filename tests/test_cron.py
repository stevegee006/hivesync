"""Cron parsing, validation and the fire-time preview.

The preview asks the same trigger object the scheduler will use, so these also
pin down what the scheduler does, not just what the editor displays.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.jobs import cron


def test_valid_expression_parses() -> None:
    assert cron.build_trigger("30 2 * * *", "UTC") is not None


@pytest.mark.parametrize(
    "expression",
    ["not a cron", "* * * *", "99 * * * *", "* * * * * *", "@daily"],
)
def test_invalid_expressions_are_refused_with_help(expression: str) -> None:
    """A schedule that cannot be parsed must not be savable: it would sit there
    looking configured and never fire."""
    with pytest.raises(cron.CronError) as excinfo:
        cron.build_trigger(expression, "UTC")
    message = str(excinfo.value)
    assert "five fields" in message
    # The message has to show what a good one looks like.
    assert "30 2 * * *" in message


def test_unknown_timezone_is_refused() -> None:
    with pytest.raises(cron.CronError, match="not a known timezone"):
        cron.build_trigger("0 2 * * *", "Not/AZone")


def test_empty_expression_is_refused_by_build_but_allowed_by_validate() -> None:
    """No schedule is a legitimate state: the job runs on demand only."""
    with pytest.raises(cron.CronError):
        cron.build_trigger("", "UTC")
    cron.validate(None, "UTC")
    cron.validate("   ", "UTC")


def test_preview_returns_five_ascending_times() -> None:
    now = datetime(2026, 3, 1, 12, 0, 30, tzinfo=UTC)
    fires = cron.next_fire_times("*/2 * * * *", "UTC", now=now)
    assert len(fires) == 5
    assert fires == sorted(fires)
    assert [moment.minute for moment in fires] == [2, 4, 6, 8, 10]


def test_a_time_exactly_on_the_boundary_counts_as_the_next_fire() -> None:
    """At exactly 12:00:00 a */2 schedule matches now, so the preview shows it.

    Pinned because it looks like an off-by-one otherwise, and because the
    scheduler behaves the same way: the preview asks the same trigger object.
    """
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    fires = cron.next_fire_times("*/2 * * * *", "UTC", now=now)
    assert [moment.minute for moment in fires] == [0, 2, 4, 6, 8]


def test_preview_is_in_the_jobs_timezone() -> None:
    """A schedule means 2:30 AM where the operator lives, not 2:30 AM UTC."""
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    fires = cron.next_fire_times("30 2 * * *", "America/Denver", now=now)
    assert fires[0].tzinfo is not None
    assert fires[0].astimezone(ZoneInfo("America/Denver")).hour == 2
    assert fires[0].astimezone(ZoneInfo("America/Denver")).minute == 30


def test_preview_across_a_dst_transition_still_fires_daily() -> None:
    """America/Denver springs forward on 2026-03-08. A daily 2:30 job must still
    produce five consecutive days rather than skipping or duplicating."""
    now = datetime(2026, 3, 6, 12, 0, tzinfo=ZoneInfo("America/Denver"))
    fires = cron.next_fire_times("30 2 * * *", "America/Denver", now=now, count=5)
    days = [moment.astimezone(ZoneInfo("America/Denver")).day for moment in fires]
    assert days == [7, 8, 9, 10, 11]


def test_preview_object_reports_errors_without_raising() -> None:
    """It renders next to an input box, so it must never blow up the page."""
    result = cron.preview("nonsense", "UTC")
    assert result.valid is False
    assert result.error is not None
    assert result.fire_times == []


def test_preview_of_no_schedule_is_valid_and_empty() -> None:
    result = cron.preview(None, "UTC")
    assert result.valid is True
    assert result.fire_times == []


def test_formatted_preview_includes_the_zone() -> None:
    result = cron.preview("30 2 * * *", "America/Denver")
    assert result.valid is True
    assert len(result.formatted) == 5
    assert any(("MST" in line or "MDT" in line) for line in result.formatted)
