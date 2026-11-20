from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from video_analysis.core.web.schemas import View
from video_analysis.modules.scheduling.task.models import TaskKind

AiKind = Literal["listen", "recognize", "summarize"]
"""可配置模型和提示词的 AI 任务类型。"""


class ProviderIn(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1, description="LangChain 供应商标识，例如 openai")
    base_url: str | None = None
    credential_ref: str | None = Field(None, description="保存密钥的环境变量名")


class ProviderView(ProviderIn, View):
    id: UUID


class ModelIn(BaseModel):
    provider_id: UUID
    name: str = Field(min_length=1)
    max_concurrency: int = Field(4, ge=1)
    requests_per_minute: int = Field(60, ge=1)
    timeout_seconds: float = Field(120, gt=0)
    params: dict = {}


class ModelView(ModelIn, View):
    id: UUID


class RouteIn(BaseModel):
    model_id: UUID
    share: int = Field(1, ge=1, description="目标份额")


class RouteView(RouteIn, View):
    kind: TaskKind
