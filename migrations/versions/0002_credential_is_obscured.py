"""Add credential.is_obscured.

Records whether a stored secret is already in rclone's obscured form, which is
the case for anything imported from a pasted rclone.conf stanza. Without this
flag such a value would be obscured a second time at run time and rclone would
reject it.

server_default is set even though no credential rows can exist yet: v0.1.0
shipped without a credential API, but a NOT NULL column added with no default
fails the moment a table is populated, and that is not a trap worth leaving.
Alembic does not compare server defaults by default, so this does not drift from
the model's Python side default.

Revision ID: 0002_credential_is_obscured
Revises: 0001_baseline
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_credential_is_obscured"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("credential", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_obscured",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("credential", schema=None) as batch_op:
        batch_op.drop_column("is_obscured")
