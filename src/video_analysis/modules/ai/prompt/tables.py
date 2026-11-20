from sqlalchemy import Boolean, Column, Integer, Table, Text, UniqueConstraint, Uuid

from video_analysis.core.database.mapping import enum_column, mapper_registry, metadata, timestamp
from video_analysis.modules.ai.prompt.models import SystemPrompt
from video_analysis.modules.scheduling.task.models import TaskKind

ai_prompts = Table(
    "ai_prompts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", enum_column(TaskKind), nullable=False),
    Column("version", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", timestamp(), nullable=False),
    UniqueConstraint("kind", "version"),
)

mapper_registry.map_imperatively(SystemPrompt, ai_prompts)
