from sqlalchemy import Column, Float, Integer, String, Table, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from video_analysis.core.database.mapping import enum_column, mapper_registry, metadata, reference
from video_analysis.modules.ai.config.models import AiModel, AiProvider, AiRoute
from video_analysis.modules.scheduling.task.models import TaskKind

ai_providers = Table(
    "ai_providers",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("provider", String(64), nullable=False),
    Column("base_url", Text),
    Column("credential_ref", String(128)),
)

ai_models = Table(
    "ai_models",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_id", reference("ai_providers.id"), nullable=False),
    Column("name", String(128), nullable=False),
    Column("max_concurrency", Integer, nullable=False),
    Column("requests_per_minute", Integer, nullable=False),
    Column("timeout_seconds", Float, nullable=False),
    Column("params", JSONB, nullable=False),
)

ai_routes = Table(
    "ai_routes",
    metadata,
    Column("kind", enum_column(TaskKind), primary_key=True),
    Column("model_id", reference("ai_models.id"), nullable=False),
    Column("share", Integer, nullable=False),
)

mapper_registry.map_imperatively(AiProvider, ai_providers)
mapper_registry.map_imperatively(AiModel, ai_models)
mapper_registry.map_imperatively(AiRoute, ai_routes)
