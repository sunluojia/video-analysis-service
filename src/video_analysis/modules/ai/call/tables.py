from sqlalchemy import Boolean, Column, Index, Integer, String, Table, Text, Uuid, false, text

from video_analysis.core.database.mapping import enum_column, metadata, reference, timestamp
from video_analysis.modules.ai.call.models import CallOutcome
from video_analysis.modules.scheduling.task.models import TaskKind

ai_calls = Table(
    "ai_calls",
    metadata,
    Column("id", Uuid, primary_key=True),
    # 任务被取消或重新分析后阶段任务会删除，调用记录仍然保留，因此不设外键。
    Column("task_id", Uuid, nullable=False),
    Column("job_id", Uuid, nullable=False, index=True),
    Column("kind", enum_column(TaskKind), nullable=False),
    Column("model_id", Uuid, nullable=False),
    Column("model", String(200)),
    Column("prompt_id", reference("ai_prompts.id")),
    Column("worker_id", String(128)),
    Column("ai_batch", Boolean, nullable=False, server_default=false()),
    Column("started_at", timestamp(), nullable=False),
    Column("deadline", timestamp(), nullable=False),
    Column("finished_at", timestamp()),
    Column("outcome", enum_column(CallOutcome)),
    Column("error", Text),
    # 成功的调用记下服务商报告的用量，页面据此算缓存命中率；没报告的为空。
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cached_tokens", Integer),
    Index("ix_ai_calls_open", "model_id", postgresql_where=text("finished_at IS NULL")),
    Index("ix_ai_calls_recent", "model_id", "started_at"),
)
