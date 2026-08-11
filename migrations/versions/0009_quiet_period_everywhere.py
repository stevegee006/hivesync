"""Apply the quiet period to every run, without changing existing jobs.

`quiet_period_seconds` was passed as `--min-age` only when a job was continuous.
It now applies to every run, which is what the setting is for: a download client
writing into the source directory does not know that a schedule fired.

Every job that predates this carries the old column default of 30 seconds,
sitting unused. Switching it on for them would silently change what a schedule
copies, so non-continuous jobs are set to 0 and keep behaving exactly as before.

New jobs now default to 0 as well: the setting applies to a manual run too, and
a non-zero default means creating files and pressing Run copies nothing. That
was measured rather than reasoned about, by ten integration tests that create a
file and sync it immediately.

Continuous jobs are untouched: the value was already in force for them, and a
watcher polling every minute is the case it exists for.

Revision ID: 0009_quiet_period_everywhere
Revises: 0008_drop_lftp_engine
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_quiet_period_everywhere"
down_revision = "0008_drop_lftp_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE job SET quiet_period_seconds = 0 WHERE continuous = 0"))


def downgrade() -> None:
    """Nothing to undo. The value was inert for these jobs before this change."""
