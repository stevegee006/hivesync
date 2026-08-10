"""Drop the lftp engine.

It was carried as an option from M0 and never built: jobs selecting it were
refused with a message. SPEC open question 1 asked whether segmented transfers
were worth having, and the answer is no, so the option and the binary both go.

Any job still recording it is rewritten to rclone. Leaving the value in place
would break loading that row once the enum no longer has the member, and the
job could not have run with it anyway.

Revision ID: 0008_drop_lftp_engine
Revises: 0007_forced_max_delete
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_drop_lftp_engine"
down_revision = "0007_forced_max_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE job SET engine = 'rclone' WHERE engine = 'lftp'"))


def downgrade() -> None:
    """Nothing to undo. The rows were unusable as lftp and rclone is the only
    engine that exists, so restoring the value would only break them again."""
