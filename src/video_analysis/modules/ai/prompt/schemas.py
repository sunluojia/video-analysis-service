from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from video_analysis.core.web.schemas import View
from video_analysis.modules.scheduling.task.models import TaskKind


class PromptIn(BaseModel):
    content: str = Field(min_length=1)
    note: str = ""
    activate: bool = True


class PromptView(View):
    id: UUID
    kind: TaskKind
    version: int
    content: str
    note: str
    active: bool
    created_at: datetime


class PromptHistory(BaseModel):
    builtin: str
    versions: list[PromptView]


class ActivePrompt(BaseModel):
    prompt_id: UUID | None = Field(description="为空表示恢复内置提示词")
