"""执行任务依赖的外部能力。具体实现在 integrations 中，可替换。"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from video_analysis.modules.ai.call.models import AiProfile, Usage
from video_analysis.modules.analysis.job.models import Material, MediaInfo
from video_analysis.modules.analysis.job.results import CreatorTags, Listening, Recognition


class Materials(Protocol):
    async def download(self, material: Material, target: Path) -> None:
        """下载到 target，并按大小和 SHA-256 校验；不一致时报错。"""
        ...


class Media(Protocol):
    """媒体处理。抽帧和切分音频的参数影响分析结果，参与判断结果能否复用。"""

    frame_count: int
    segment_seconds: int

    async def probe(self, source: Path) -> MediaInfo: ...
    async def extract_frames(self, source: Path, target: Path) -> int:
        """在整段视频上均匀抽取图片，写入 target，返回图片数。"""
        ...

    async def extract_audio(self, source: Path, target: Path) -> int:
        """把可听音频段按 000.mp3 起依次写入 target，返回段数。"""
        ...


@dataclass(frozen=True)
class VideoMaterial:
    frames: list[Path]
    listening: Listening
    description: str
    topics: list[str]


class Rejected(Exception):
    """模型接口拒绝了请求本身，如内容审核不通过、音频为空、参数无效；原样重发结果相同，不再重试。"""


class InvalidAudio(Rejected):
    """模型以 HTTP 400 拒绝了空音频或非法格式的听音请求。"""


@dataclass(frozen=True)
class Answer[T]:
    """模型的回答和这次调用的 token 用量；服务商没有报告用量时为 None。"""

    value: T
    usage: Usage | None = None


class VideoAi(Protocol):
    """请求被拒绝时抛出 Rejected；限流、超时和服务端错误照常抛出，可以重试。"""

    async def listen(self, profile: AiProfile, audio: Path) -> Answer[Listening]: ...
    async def recognize(
        self, profile: AiProfile, material: VideoMaterial
    ) -> Answer[Recognition]: ...
    async def summarize(
        self, profile: AiProfile, videos: Sequence[Recognition]
    ) -> Answer[CreatorTags]: ...
