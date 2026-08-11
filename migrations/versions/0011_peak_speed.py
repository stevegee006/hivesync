"""Record how fast each file actually moved.

Per-file speed appears only in the `transferring` array of rclone's stats
events, while the file is in flight, and was thrown away when the run ended.
The history screen needs it afterwards.

NULL where nothing measured it, which is most rows: a dry run moves nothing, a
deletion transfers nothing, and a file small enough to start and finish between
two stats ticks never appears in `transferring` at all. NULL means "not
measured" and is rendered as blank rather than as a zero.

Revision ID: 0011_peak_speed
Revises: 0010_archive_dirs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_peak_speed"
down_revision = "0010_archive_dirs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_run_change", sa.Column("peak_speed_bps", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_run_change", "peak_speed_bps")
