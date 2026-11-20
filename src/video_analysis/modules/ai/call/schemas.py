from datetime import datetime

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
