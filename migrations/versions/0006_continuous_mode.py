"""Add continuous mode columns to job.

Polling with a backoff, not push: no backend HiveSync supports reports
ChangeNotify. See app/jobs/watcher.py.

Revision ID: 0006_continuous_mode
Revises: 0005_login_attempts
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models.base

revision: str = "0006_continuous_mode"
down_revision: str | None = "0005_login_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("continuous", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column(
                "continuous_interval_seconds",
                sa.Integer(),
                nullable=False,
                server_default="60",
            )
        )
        batch_op.add_column(
            sa.Column(
                "continuous_idle_interval_seconds",
                sa.Integer(),
                nullable=False,
                server_default="900",
            )
        )
        batch_op.add_column(
            sa.Column("quiet_period_seconds", sa.Integer(), nullable=False, server_default="30")
        )
        batch_op.add_column(
            sa.Column("last_checked_at", app.models.base.UtcDateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_column("last_checked_at")
        batch_op.drop_column("quiet_period_seconds")
        batch_op.drop_column("continuous_idle_interval_seconds")
        batch_op.drop_column("continuous_interval_seconds")
        batch_op.drop_column("continuous")
