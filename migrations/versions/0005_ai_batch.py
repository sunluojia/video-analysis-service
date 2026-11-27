"""Persist Bailian Batch mode, remote jobs and submitted task leases.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, column in (
        ("batches", "ai_batch_mode"),
        ("tasks", "ai_batch_fallback"),
        ("ai_calls", "ai_batch"),
    ):
        op.add_column(
            table, sa.Column(column, sa.Boolean, nullable=False, server_default=sa.false())
        )
    op.create_table(
        "ai_batch_jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("model_id", sa.Uuid, nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("task_kind", sa.String(32), nullable=False),
        sa.Column("profile_snapshot", JSONB, nullable=False),
        sa.Column("remote_file_id", sa.String(256)),
        sa.Column("remote_batch_id", sa.String(256), unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("request_count", sa.Integer, nullable=False),
        sa.Column("completed_count", sa.Integer, nullable=False),
        sa.Column("failed_count", sa.Integer, nullable=False),
        sa.Column("output_file_id", sa.String(256)),
        sa.Column("error_file_id", sa.String(256)),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_ai_batch_jobs_active",
        "ai_batch_jobs",
        ["created_at"],
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_table(
        "ai_batch_items",
        sa.Column("custom_id", sa.String(256), primary_key=True),
        sa.Column(
            "batch_job_id",
            sa.Uuid,
            sa.ForeignKey("ai_batch_jobs.id", deferrable=True, initially="DEFERRED"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("lease_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("model_id", sa.Uuid, nullable=False),
        sa.Column("prompt_version", sa.Integer),
        sa.Column("frame_count", sa.Integer),
        sa.Column("line_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ai_batch_items_batch_job_id", "ai_batch_items", ["batch_job_id"])


def downgrade() -> None:
    op.drop_table("ai_batch_items")
    op.drop_table("ai_batch_jobs")
    op.drop_column("ai_calls", "ai_batch")
    op.drop_column("tasks", "ai_batch_fallback")
    op.drop_column("batches", "ai_batch_mode")
