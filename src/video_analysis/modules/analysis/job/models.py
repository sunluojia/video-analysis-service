from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from video_analysis.core.common.clock import utcnow
from video_analysis.modules.analysis.job.results import Recognition


class JobKind(StrEnum):
    VIDEO = "video"
    SUMMARY = "summary"


class JobStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Material(BaseModel):
    """调度中心登记的原视频：按 ID 下载，下载后按大小和 SHA-256 校验。"""

    id: str
    size: int = Field(gt=0)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")


class VideoInput(BaseModel):
    material: Material
    description: str = ""
    topics: list[str] = []
    allow_reuse: bool = True
    """相同素材、相同输入和相同模型配置已有结果时直接复用；重新分析时调度中心传 False。"""


class SourceVideo(BaseModel):
    """参与作者汇总的一条视频结果。"""

    job_id: str
    run: int
    recognition: Recognition


class SummaryInput(BaseModel):
    videos: list[SourceVideo] = Field(min_length=1)


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool


@dataclass
class Job:
    kind: JobKind
    external_id: str
    """调度中心的任务 ID。"""
    run: int
    """分析轮次；重新分析时由调度中心递增。"""
    batch_id: str
    creator_id: str
    inputs: dict
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.ACCEPTED
    duration: float | None = None
    frame_count: int | None = None
    segment_count: int | None = None
    """待听音的音频段数；None 表示音频尚未准备，0 表示无音轨或数字静音。"""
    result: dict | None = None
    error: str | None = None
    analysis_key: str | None = None
    """素材、输入和模型配置的摘要，相同时可以复用结果。"""
    reused_from: str | None = None
    accepted_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    artifacts_until: datetime | None = None
    """抽帧图片和音频段保留到此时；None 表示仍在处理或已经清理。"""

    @property
    def video(self) -> VideoInput:
        return VideoInput.model_validate(self.inputs)

    @property
    def summary(self) -> SummaryInput:
        return SummaryInput.model_validate(self.inputs)

    def restart(self, run: int, inputs: dict, now: datetime) -> None:
        """新一轮分析：丢弃上一轮的进度和结果。"""
        self.run, self.inputs = run, inputs
        self.status = JobStatus.ACCEPTED
        self.duration = self.frame_count = self.segment_count = None
        self.result = self.error = self.analysis_key = self.reused_from = None
        self.accepted_at = now
        self.finished_at = self.artifacts_until = None

    def start(self) -> None:
        self.status = JobStatus.PROCESSING
        self.error = None

    def fetched(self, info: MediaInfo) -> None:
        self.duration = info.duration
        if not info.has_audio:
            self.segment_count = 0

    def ready_for_recognition(self, listened_segments: int) -> bool:
        """图片已抽取，且所需的听音结果全部齐备。"""
        return self.frame_count is not None and self.segment_count == listened_segments

    def complete(self, result: dict, now: datetime) -> None:
        self.status = JobStatus.COMPLETED
        self.result = result
        self.error = None
        self.finished_at = now

    def reuse(self, earlier: Self, now: datetime) -> None:
        """采用之前相同分析的结果，不再下载和调用模型。"""
        self.duration = earlier.duration
        self.analysis_key = earlier.analysis_key
        self.reused_from = earlier.external_id
        self.complete(earlier.result or {}, now)

    def fail(self, error: str, now: datetime) -> None:
        self.status = JobStatus.FAILED
        self.error = error
        self.finished_at = now

    def hold_for_review(self, error: str, now: datetime) -> None:
        """失败优先于待核查：已失败的任务保持失败。"""
        if self.status != JobStatus.FAILED:
            self.status = JobStatus.NEEDS_REVIEW
            self.error = error
            self.finished_at = now

    @property
    def artifacts_discarded(self) -> bool:
        """已结束的视频任务，抽帧图片和音频段过了保留期已经清理。"""
        return (
            self.kind == JobKind.VIDEO
            and self.finished_at is not None
            and self.artifacts_until is None
        )

    def prepare_again(self) -> None:
        """补跑时重新获取视频、抽帧和听音。"""
        self.duration = self.frame_count = self.segment_count = self.analysis_key = None

    def reopen(self) -> None:
        """补跑：从已保存的阶段继续。"""
        self.status = JobStatus.PROCESSING
        self.error = None
        self.finished_at = self.artifacts_until = None

    def cancel(self, now: datetime) -> None:
        """取消后不再推进，派生文件随即清理。"""
        self.status = JobStatus.CANCELLED
        self.finished_at = self.artifacts_until = now
