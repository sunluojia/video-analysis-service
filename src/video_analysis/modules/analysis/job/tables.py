from sqlalchemy import Column, Float, Index, Integer, String, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from video_analysis.core.database.mapping import (
    enum_column,
    mapper_registry,
    metadata,
    reference,
    timestamp,
)
from video_analysis.modules.analysis.job.models import Job, JobKind, JobStatus

jobs = Table(
    "jobs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", enum_column(JobKind), nullable=False),
    Column("external_id", String(128), nullable=False, unique=True),
    Column("run", Integer, nullable=False),
    Column("batch_id", reference("batches.id"), nullable=False, index=True),
    Column("creator_id", String(128), nullable=False),
    Column("inputs", JSONB, nullable=False),
    Column("status", enum_column(JobStatus), nullable=False, index=True),
    Column("duration", Float),
    Column("frame_count", Integer),
    Column("segment_count", Integer),
    Column("result", JSONB),
    Column("error", Text),
    Column("analysis_key", String(64), index=True),
    Column("reused_from", String(128)),
    Column("accepted_at", timestamp(), nullable=False),
    Column("finished_at", timestamp()),
    Column("artifacts_until", timestamp()),
    Index(
        "ix_jobs_artifacts_until",
        "artifacts_until",
        postgresql_where=text("artifacts_until IS NOT NULL"),
    ),
)

mapper_registry.map_imperatively(Job, jobs)
