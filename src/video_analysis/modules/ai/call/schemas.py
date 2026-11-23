from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from video_analysis.modules.ai.call.models import CallOutcome
from video_analysis.modules.scheduling.task.models import TaskKind


class AiCallView(BaseModel):
    kind: TaskKind
    model: str | None
    prompt_version: int | None
    """None 表示内置提示词。"""
    started_at: datetime
    finished_at: datetime | None
    outcome: CallOutcome | None
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None


class UsageRow(BaseModel):
    """一个时段内一类任务在一个模型上的调用汇总。"""

    at: datetime
    """时段开始：北京时间的整点或零点。"""
    kind: TaskKind
    model_id: UUID
    model: str | None
    calls: int
    """发出的调用，包括还没结束的。"""
    succeeded: int
    failed: int
    """失败、超时和结果不明的调用。"""
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    """命中缓存的输入 token。"""
    cache_input_tokens: int
    """报告了命中数的调用的输入 token；缓存命中率 = cached_tokens ÷ 它。"""
    busy_seconds: float
    """调用占用模型的时长合计，除以时段长度就是平均同时在途的请求数。"""
