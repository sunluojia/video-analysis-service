from dataclasses import dataclass, field
from uuid import UUID, uuid4

from video_analysis.modules.scheduling.task.models import TaskKind


@dataclass
class AiProvider:
    name: str
    provider: str
    """LangChain 供应商标识，例如 openai、anthropic、google_genai。"""
    base_url: str | None = None
    credential_ref: str | None = None
    """保存密钥的环境变量名，数据库不保存密钥本身。"""
    id: UUID = field(default_factory=uuid4)


@dataclass
class AiModel:
    """一个独立限额单位：在途请求上限和每分钟请求数由所有 Worker 共用。"""

    provider_id: UUID
    name: str
    max_concurrency: int = 4
    requests_per_minute: int = 60
    timeout_seconds: float = 120
    params: dict = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)


@dataclass
class AiRoute:
    """一类 AI 任务使用的模型及其目标份额。"""

    kind: TaskKind
    model_id: UUID
    share: int = 1
