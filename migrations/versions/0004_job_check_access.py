"""Add job.check_access and job_run.is_resync.

Opt-in for bisync's --check-access, which aborts unless matching RCLONE_TEST
files are present on both sides. It is bisync's own guard against a half-mounted
endpoint, and unlike the sentinel file of SPEC 6.4 it works on SMB and SFTP
rather than only on local paths.

Off by default: enabling it breaks a job until the operator places the marker
files, which would be a baffling first experience for a feature nobody asked for.

Revision ID: 0004_job_check_access
Revises: 0003_host_keys
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_job_check_access"
down_revision: str | None = "0003_host_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("check_access", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("job_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_resync", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("job_run", schema=None) as batch_op:
        batch_op.drop_column("is_resync")
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_column("check_access")
