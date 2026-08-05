"""Replace connection.host_key_fingerprint with connection.host_keys.

Two problems with the original column. It held a single key, but a server
commonly offers several (ssh-rsa and ssh-ed25519 here) and the client negotiates
one of them, so pinning one silently forces that algorithm and breaks when the
server stops offering it. And it was String(255), which a single RSA entry
already exceeds; SQLite does not enforce the length, so the schema was quietly
lying.

The new column is Text and holds one known_hosts style "type base64" entry per
line. Existing values are dropped rather than migrated: a lone pinned key is
exactly the state this change exists to fix, and re-approving is a deliberate,
reviewable action. There are no rows in practice, since M0 shipped without a
connections API.

Revision ID: 0003_host_keys
Revises: 0002_credential_is_obscured
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_host_keys"
down_revision: str | None = "0002_credential_is_obscured"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("connection", schema=None) as batch_op:
        batch_op.add_column(sa.Column("host_keys", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "host_keys_trusted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.drop_column("host_key_fingerprint")


def downgrade() -> None:
    with op.batch_alter_table("connection", schema=None) as batch_op:
        batch_op.add_column(sa.Column("host_key_fingerprint", sa.String(length=255), nullable=True))
        batch_op.drop_column("host_keys_trusted")
        batch_op.drop_column("host_keys")
