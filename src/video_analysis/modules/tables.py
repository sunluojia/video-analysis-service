"""全部表定义的汇总，数据库迁移和测试据此得到完整的表结构。"""

from video_analysis.core.database.mapping import metadata
from video_analysis.core.messaging.outbox import outbox
from video_analysis.modules.ai.batch.tables import ai_batch_items, ai_batch_jobs
from video_analysis.modules.ai.call.tables import ai_calls
from video_analysis.modules.ai.config.tables import ai_models, ai_providers, ai_routes
from video_analysis.modules.ai.prompt.tables import ai_prompts
from video_analysis.modules.analysis.batch.tables import batches
from video_analysis.modules.analysis.job.tables import jobs
from video_analysis.modules.scheduling.task.tables import analysis_attempts, tasks

__all__ = [
    "ai_batch_items",
    "ai_batch_jobs",
    "ai_calls",
    "ai_models",
    "ai_prompts",
    "ai_providers",
    "ai_routes",
    "analysis_attempts",
    "batches",
    "jobs",
    "metadata",
    "outbox",
    "tasks",
]
