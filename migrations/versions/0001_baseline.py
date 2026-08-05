"""Baseline schema: the full data model from SPEC.md section 4.

Generated with `alembic revision --autogenerate` from app/models, then given a
readable revision id. tests/test_migrations.py asserts that applying this file
produces exactly the declarative models, so the two cannot drift apart.

Three things here are not in SPEC section 4 and are explained in the model
modules: the source_path rename, the job_filter_preset association table, and the
partial unique index that enforces one active run per job.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "credential",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "password",
                "ssh_key",
                "smb_ntlm",
                "backend_secret",
                name="credentialkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_passphrase_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "filter_preset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("rules", sqlite.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role", sa.Enum("admin", name="userrole", native_enum=False, length=16), nullable=False
        ),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "connection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "local",
                "sftp",
                "ftp",
                "ftps",
                "smb",
                "rclone_remote",
                name="connectiontype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("share", sa.String(length=255), nullable=True),
        sa.Column("base_path", sa.Text(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("extra_opts", sqlite.JSON(), nullable=False),
        sa.Column(
            "rclone_mode",
            sa.Enum("inline", "imported", name="rclonemode", native_enum=False, length=16),
            nullable=True,
        ),
        sa.Column("rclone_remote_name", sa.String(length=128), nullable=True),
        sa.Column("rclone_backend_type", sa.String(length=64), nullable=True),
        sa.Column("capabilities", sqlite.JSON(), nullable=True),
        sa.Column("capabilities_probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sentinel_file", sa.String(length=255), nullable=True),
        sa.Column("host_key_fingerprint", sa.String(length=255), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["credential.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source_connection_id", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("dest_connection_id", sa.Integer(), nullable=False),
        sa.Column("dest_path", sa.Text(), nullable=False),
        sa.Column(
            "engine",
            sa.Enum("rclone", "lftp", name="engine", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "direction",
            sa.Enum(
                "source_to_dest",
                "dest_to_source",
                "bidirectional",
                name="direction",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "delete_mode",
            sa.Enum("none", "delete", "archive", name="deletemode", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("archive_base", sa.Text(), nullable=True),
        sa.Column(
            "archive_layout",
            sa.Enum(
                "timestamped_dir", "suffix", name="archivelayout", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("archive_retention_days", sa.Integer(), nullable=True),
        sa.Column("filters", sqlite.JSON(), nullable=False),
        sa.Column(
            "compare_mode",
            sa.Enum(
                "mtime_size",
                "checksum",
                "size_only",
                name="comparemode",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("modify_window", sa.String(length=16), nullable=False),
        sa.Column("transfers", sa.Integer(), nullable=True),
        sa.Column("checkers", sa.Integer(), nullable=True),
        sa.Column("bwlimit", sa.String(length=32), nullable=True),
        sa.Column("max_delete_pct", sa.Integer(), nullable=False),
        sa.Column(
            "conflict_resolve",
            sa.Enum(
                "newer",
                "older",
                "larger",
                "smaller",
                "path1",
                "path2",
                "none",
                name="conflictresolve",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("schedule_cron", sa.String(length=128), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "notify_on",
            sa.Enum("never", "failure", "always", name="notifyon", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("bisync_initialized", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dest_connection_id"], ["connection.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_connection_id"], ["connection.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "job_filter_preset",
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("filter_preset_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["filter_preset_id"], ["filter_preset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "filter_preset_id"),
    )
    op.create_table(
        "job_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column(
            "trigger",
            sa.Enum("manual", "schedule", "api", name="runtrigger", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.Enum("dry_run", "live", name="runmode", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "success",
                "failed",
                "cancelled",
                "skipped",
                name="runstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("files_transferred", sa.Integer(), nullable=False),
        sa.Column("files_deleted", sa.Integer(), nullable=False),
        sa.Column("files_archived", sa.Integer(), nullable=False),
        sa.Column("bytes_transferred", sa.BigInteger(), nullable=False),
        sa.Column("errors_count", sa.Integer(), nullable=False),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("summary", sqlite.JSON(), nullable=True),
        sa.Column("command_redacted", sa.Text(), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("job_run", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_run_job_id"), ["job_id"], unique=False)
        batch_op.create_index("ix_job_run_job_started", ["job_id", "started_at"], unique=False)
        batch_op.create_index(
            "uq_job_run_active_per_job",
            ["job_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'running')"),
        )

    op.create_table(
        "job_run_change",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "new",
                "updated",
                "deleted",
                "archived",
                "unchanged",
                "conflict",
                "error",
                name="changeaction",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "side",
            sa.Enum("source", "dest", name="changeside", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["job_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("job_run_change", schema=None) as batch_op:
        batch_op.create_index("ix_job_run_change_run_action", ["run_id", "action"], unique=False)
        batch_op.create_index(batch_op.f("ix_job_run_change_run_id"), ["run_id"], unique=False)


def downgrade() -> None:

    with op.batch_alter_table("job_run_change", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_run_change_run_id"))
        batch_op.drop_index("ix_job_run_change_run_action")

    op.drop_table("job_run_change")
    with op.batch_alter_table("job_run", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_job_run_active_per_job", sqlite_where=sa.text("status IN ('queued', 'running')")
        )
        batch_op.drop_index("ix_job_run_job_started")
        batch_op.drop_index(batch_op.f("ix_job_run_job_id"))

    op.drop_table("job_run")
    op.drop_table("job_filter_preset")
    op.drop_table("job")
    op.drop_table("connection")
    op.drop_table("user")
    op.drop_table("setting")
    op.drop_table("filter_preset")
    op.drop_table("credential")
