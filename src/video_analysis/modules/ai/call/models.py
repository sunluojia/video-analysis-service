from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

BAILIAN_EXPLICIT_CACHE = "bailian_explicit_cache"


class CallOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LOST = "lost"
    """Worker 在等待响应时中断，无法确认模型是否已处理。"""


@dataclass(frozen=True)
class AiProfile:
    """领取任务时确定的调用配置。执行期间不再读取路由，请求发给占用额度的同一个模型。"""

    model_id: UUID
    provider: str
    model: str
    base_url: str | None
    credential_ref: str | None
    params: Mapping
    timeout_seconds: float
    prompt: str
    prompt_id: UUID | None
    """生效的提示词版本；None 表示内置提示词。"""
    prompt_version: int | None

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def model_params(self) -> dict:
        """传给模型客户端的参数。缓存模式由模型接入层处理。"""
        params = dict(self.params)
        params.pop(BAILIAN_EXPLICIT_CACHE, None)
        return params

    @property
    def fingerprint(self) -> dict:
        """影响模型输出的配置，用于判断结果能否复用。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "params": self.model_params,
            "prompt": self.prompt,
        }
