"""Record where a run archived its deletions, so they can be restored.

The archive directory carries the run timestamp, computed when the run starts,
so it cannot be derived from the run record afterwards. It was only ever emitted
as a status line, which meant nothing could find it again.

Stored without the remote prefix: a run addresses its endpoints through
synthetic aliases that exist only for that process, and a stored spec would name
a remote that no longer exists.

NULL for every existing run. Those archives are still on disk at the path the
job's settings describe, and the run page says so rather than offering a restore
it cannot target precisely.

Revision ID: 0010_archive_dirs
Revises: 0009_quiet_period_everywhere
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.sqlite import JSON

revision = "0010_archive_dirs"
down_revision = "0009_quiet_period_everywhere"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_run", sa.Column("archive_dirs", JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_run", "archive_dirs")
