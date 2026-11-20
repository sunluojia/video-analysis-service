from sqlalchemy import Column, Index, Integer, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from video_analysis.core.database.mapping import (
    enum_column,
    mapper_registry,
    metadata,
    reference,
    timestamp,
)
from video_analysis.modules.scheduling.task.models import Task, TaskKind, TaskStatus

tasks = Table(
    "tasks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", enum_column(TaskKind), nullable=False),
    Column("job_id", reference("jobs.id"), nullable=False, index=True),
    Column("segment", Integer),
    Column("status", enum_column(TaskStatus), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("run_after", timestamp(), nullable=False),
    Column("started_at", timestamp()),
    Column("lease_id", Uuid),
    Column("lease_expires_at", timestamp()),
    Column("result", JSONB),
    Column("error", Text),
    Column("created_at", timestamp(), nullable=False),
    Column("finished_at", timestamp()),
    Index("ix_tasks_ready", "kind", "run_after", postgresql_where=text("status = 'ready'")),
    Index("ix_tasks_running", "lease_expires_at", postgresql_where=text("status = 'running'")),
)

mapper_registry.map_imperatively(Task, tasks)
