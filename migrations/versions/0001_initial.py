"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TIME = sa.DateTime(timezone=True)
STATE = sa.String(32)
LABEL = sa.String(128)


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", LABEL),
        sa.Column("paused", sa.Boolean, nullable=False),
        sa.Column("last_dispatched_at", TIME),
        sa.PrimaryKeyConstraint("id", name="pk_batches"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid),
        sa.Column("kind", STATE, nullable=False),
        sa.Column("external_id", LABEL, nullable=False),
        sa.Column("run", sa.Integer, nullable=False),
        sa.Column("batch_id", LABEL, nullable=False),
        sa.Column("creator_id", LABEL, nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("status", STATE, nullable=False),
        sa.Column("duration", sa.Float),
        sa.Column("frame_count", sa.Integer),
        sa.Column("segment_count", sa.Integer),
        sa.Column("result", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("analysis_key", sa.String(64)),
        sa.Column("reused_from", LABEL),
        sa.Column("accepted_at", TIME, nullable=False),
        sa.Column("finished_at", TIME),
        sa.Column("artifacts_until", TIME),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batches.id"],
            name="fk_jobs_batch_id_batches",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("external_id", name="uq_jobs_external_id"),
    )
    op.create_index("ix_jobs_batch_id", "jobs", ["batch_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_analysis_key", "jobs", ["analysis_key"])
    op.create_index(
        "ix_jobs_artifacts_until",
        "jobs",
        ["artifacts_until"],
        postgresql_where=sa.text("artifacts_until IS NOT NULL"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid),
        sa.Column("kind", STATE, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("segment", sa.Integer),
        sa.Column("status", STATE, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("run_after", TIME, nullable=False),
        sa.Column("started_at", TIME),
        sa.Column("lease_id", sa.Uuid),
        sa.Column("lease_expires_at", TIME),
        sa.Column("result", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("finished_at", TIME),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_tasks_job_id_jobs",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_tasks_job_id", "tasks", ["job_id"])
    op.create_index(
        "ix_tasks_ready",
        "tasks",
        ["kind", "run_after"],
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_index(
        "ix_tasks_running",
        "tasks",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.Uuid),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("base_url", sa.Text),
        sa.Column("credential_ref", sa.String(128)),
        sa.PrimaryKeyConstraint("id", name="pk_ai_providers"),
        sa.UniqueConstraint("name", name="uq_ai_providers_name"),
    )
    op.create_table(
        "ai_models",
        sa.Column("id", sa.Uuid),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("max_concurrency", sa.Integer, nullable=False),
        sa.Column("requests_per_minute", sa.Integer, nullable=False),
        sa.Column("timeout_seconds", sa.Float, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_models"),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_ai_models_provider_id_ai_providers",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_table(
        "ai_routes",
        sa.Column("kind", STATE),
        sa.Column("model_id", sa.Uuid, nullable=False),
        sa.Column("share", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("kind", name="pk_ai_routes"),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["ai_models.id"],
            name="fk_ai_routes_model_id_ai_models",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_table(
        "ai_prompts",
        sa.Column("id", sa.Uuid),
        sa.Column("kind", STATE, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_prompts"),
        sa.UniqueConstraint("kind", "version", name="uq_ai_prompts_kind"),
    )
    op.create_table(
        "ai_calls",
        sa.Column("id", sa.Uuid),
        sa.Column("task_id", sa.Uuid, nullable=False),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("kind", STATE, nullable=False),
        sa.Column("model_id", sa.Uuid, nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("prompt_id", sa.Uuid),
        sa.Column("started_at", TIME, nullable=False),
        sa.Column("deadline", TIME, nullable=False),
        sa.Column("finished_at", TIME),
        sa.Column("outcome", STATE),
        sa.Column("error", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_ai_calls"),
        sa.ForeignKeyConstraint(
            ["prompt_id"],
            ["ai_prompts.id"],
            name="fk_ai_calls_prompt_id_ai_prompts",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_ai_calls_job_id", "ai_calls", ["job_id"])
    op.create_index(
        "ix_ai_calls_open",
        "ai_calls",
        ["model_id"],
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_index("ix_ai_calls_recent", "ai_calls", ["model_id", "started_at"])
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger, sa.Identity()),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("published_at", TIME),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
    )
    op.create_index(
        "ix_outbox_pending", "outbox", ["id"], postgresql_where=sa.text("published_at IS NULL")
    )


def downgrade() -> None:
    for table in [
        "outbox",
        "ai_calls",
        "ai_prompts",
        "ai_routes",
        "ai_models",
        "ai_providers",
        "tasks",
        "jobs",
        "batches",
    ]:
        op.drop_table(table)
