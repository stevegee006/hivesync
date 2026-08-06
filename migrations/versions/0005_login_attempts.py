"""Add login_attempt, for login rate limiting.

Failures are counted in the database rather than in memory so a restart does not
clear a lockout. See app/ratelimit.py.

Revision ID: 0005_login_attempts
Revises: 0004_job_check_access
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models.base

revision: str = "0005_login_attempts"
down_revision: str | None = "0004_job_check_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "scope",
            sa.Enum("username", "address", name="attemptscope", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("attempted_at", app.models.base.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("login_attempt", schema=None) as batch_op:
        batch_op.create_index(
            "ix_login_attempt_scope_value_time",
            ["scope", "value", "attempted_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("login_attempt", schema=None) as batch_op:
        batch_op.drop_index("ix_login_attempt_scope_value_time")
    op.drop_table("login_attempt")
