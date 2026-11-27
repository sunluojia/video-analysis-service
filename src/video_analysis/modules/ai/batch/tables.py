from sqlalchemy import Column, Index, Integer, String, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from video_analysis.core.database.mapping import (
    enum_column,
    mapper_registry,
    metadata,
    reference,
    timestamp,
)
from video_analysis.modules.ai.batch.models import BatchItem, BatchJob
from video_analysis.modules.scheduling.task.models import TaskKind

ai_batch_jobs = Table(
    "ai_batch_jobs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("model_id", Uuid, nullable=False),
    Column("model_name", String(128), nullable=False),
    Column("task_kind", enum_column(TaskKind), nullable=False),
    Column("profile_snapshot", JSONB, nullable=False),
    Column("remote_file_id", String(256)),
    Column("remote_batch_id", String(256), unique=True),
    Column("status", String(32), nullable=False),
    Column("request_count", Integer, nullable=False),
    Column("completed_count", Integer, nullable=False),
    Column("failed_count", Integer, nullable=False),
    Column("output_file_id", String(256)),
    Column("error_file_id", String(256)),
    Column("error", Text),
    Column("created_at", timestamp(), nullable=False),
    Column("updated_at", timestamp(), nullable=False),
    Column("finished_at", timestamp()),
    Index("ix_ai_batch_jobs_active", "created_at", postgresql_where=text("finished_at IS NULL")),
)

ai_batch_items = Table(
    "ai_batch_items",
    metadata,
    Column("custom_id", String(256), primary_key=True),
    Column("batch_job_id", reference("ai_batch_jobs.id"), nullable=False, index=True),
    # Keep the raw IDs even when a new analysis run replaces its tasks.
    Column("task_id", Uuid, nullable=False),
    Column("job_id", Uuid, nullable=False),
    Column("lease_id", Uuid, nullable=False, unique=True),
    Column("model_id", Uuid, nullable=False),
    Column("prompt_version", Integer),
    Column("frame_count", Integer),
    Column("line_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("error", Text),
    Column("created_at", timestamp(), nullable=False),
    Column("finished_at", timestamp()),
)

mapper_registry.map_imperatively(BatchJob, ai_batch_jobs)
mapper_registry.map_imperatively(BatchItem, ai_batch_items)
