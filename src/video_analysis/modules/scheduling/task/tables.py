from sqlalchemy import Column, Index, Integer, String, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from video_analysis.core.database.mapping import (
    enum_column,
    mapper_registry,
    metadata,
    reference,
    timestamp,
)
from video_analysis.modules.scheduling.task.models import AttemptStatus, Task, TaskKind, TaskStatus

tasks = Table(
    "tasks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", enum_column(TaskKind), nullable=False),
    Column("job_id", reference("jobs.id"), nullable=False, index=True),
    Column("segment", Integer),
    Column("status", enum_column(TaskStatus), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("retry_generation", Integer, nullable=False, server_default=text("0")),
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

analysis_attempts = Table(
    "analysis_attempts",
    metadata,
    Column("attempt_id", Uuid, primary_key=True),
    # 保留被删除或重建的阶段任务历史，因此不设 tasks 外键。
    Column("task_id", Uuid, nullable=False),
    Column("job_external_id", String(128), nullable=False),
    Column("run", Integer, nullable=False),
    Column("retry_generation", Integer, nullable=False, server_default=text("0")),
    Column("stage", String(64), nullable=False),
    Column("segment", Integer),
    Column("attempt_no", Integer, nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("worker_session_id", Uuid, nullable=False),
    Column("started_at", timestamp(), nullable=False),
    Column("ended_at", timestamp()),
    Column("status", enum_column(AttemptStatus), nullable=False),
    Column("error", Text),
    Index("ix_analysis_attempts_job_run_stage", "job_external_id", "run", "stage"),
    Index("ix_analysis_attempts_worker_started_at", "worker_id", "started_at"),
)

mapper_registry.map_imperatively(Task, tasks)
