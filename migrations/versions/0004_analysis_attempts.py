"""记录分析尝试、Worker 归属和人工补跑代次

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("retry_generation", sa.Integer, server_default=sa.text("0"), nullable=False),
    )
    op.add_column("ai_calls", sa.Column("worker_id", sa.String(128)))
    op.create_table(
        "analysis_attempts",
        sa.Column("attempt_id", sa.Uuid, primary_key=True),
        sa.Column("task_id", sa.Uuid, nullable=False),
        sa.Column("job_external_id", sa.String(128), nullable=False),
        sa.Column("run", sa.Integer, nullable=False),
        sa.Column("retry_generation", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("segment", sa.Integer),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("worker_session_id", sa.Uuid, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text),
    )
    op.create_index(
        "ix_analysis_attempts_job_run_stage",
        "analysis_attempts",
        ["job_external_id", "run", "stage"],
    )
    op.create_index(
        "ix_analysis_attempts_worker_started_at", "analysis_attempts", ["worker_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_table("analysis_attempts")
    op.drop_column("ai_calls", "worker_id")
    op.drop_column("tasks", "retry_generation")
