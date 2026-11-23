from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

BAILIAN_EXPLICIT_CACHE = "bailian_explicit_cache"
"""模型参数里的显式缓存开关：支持的模型默认开启，设为 false 关闭。"""
EXPLICIT_CACHE_MODELS = frozenset(
    {
        "qwen4-max",
        "qwen3.8-max",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
        "qwen-plus",
        "qwen3.8-flash",
        "qwen3.7-flash",
        "qwen3.6-flash",
        "qwen3.5-flash",
        "qwen-flash",
        "qwen3-vl-plus",
    }
)
"""百炼（华北2）支持显式缓存的千问模型，见百炼文档「上下文缓存」。"""


class CallOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LOST = "lost"
    """Worker 在等待响应时中断，无法确认模型是否已处理。"""


@dataclass(frozen=True)
class Usage:
    """一次调用的 token 用量；服务商没有报告命中缓存的数量时 cached_tokens 为 None。"""

    input_tokens: int
    output_tokens: int
    cached_tokens: int | None


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
