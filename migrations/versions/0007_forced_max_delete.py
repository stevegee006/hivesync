"""Record an operator's approval of a specific number of deletions.

A run refused by the delete brake can be re-run with the deletions the check
found explicitly approved. The approval is a count rather than a flag, so an
approval given for two deletions cannot authorise twenty.

Revision ID: 0007_forced_max_delete
Revises: 0006_continuous_mode
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_forced_max_delete"
down_revision = "0006_continuous_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_run", sa.Column("forced_max_delete", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_run", "forced_max_delete")
