from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.analysis.job.models import (
    Job,
    JobKind,
    JobStatus,
    SummaryInput,
    VideoInput,
)
from video_analysis.modules.scheduling.task.models import Task, TaskKind, TaskStatus

Label = Annotated[str, Field(min_length=1, max_length=128)]


class JobMessage(BaseModel):
    job_id: Label
    """调度中心的任务 ID，重复投递按它去重。"""
    run: int = Field(ge=1)
    """分析轮次；更大的轮次表示重新分析。"""
    batch_id: Label
    creator_id: Label


class AnalyzeVideo(JobMessage, VideoInput):
    """单视频分析任务。"""


class SummarizeCreator(JobMessage, SummaryInput):
    """作者汇总任务，输入是调度中心收齐的各视频识别结果。"""


class StageStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"


STAGE_STATUS = {
    TaskStatus.RUNNING: StageStatus.STARTED,
    TaskStatus.SUCCEEDED: StageStatus.SUCCEEDED,
    TaskStatus.READY: StageStatus.RETRYING,
    TaskStatus.FAILED: StageStatus.FAILED,
    TaskStatus.NEEDS_REVIEW: StageStatus.NEEDS_REVIEW,
}
"""阶段任务状态变化后对应的事件；失败后回到就绪即为等待重试。"""


class JobEvent(BaseModel):
    """任务级事件：受理、完成、失败、待核查。"""

    type: Literal["job"] = "job"
    event_id: UUID = Field(default_factory=uuid4)
    job_id: str
    run: int
    kind: JobKind
    status: JobStatus
    occurred_at: datetime
    result: dict | None = None
    error: str | None = None
    reused_from: str | None = None
    artifacts_until: datetime | None = None
    """抽帧图片和音频段可以获取到此时。"""

    @classmethod
    def of(cls, job: Job, now: datetime) -> Self:
        return cls(
            job_id=job.external_id,
            run=job.run,
            kind=job.kind,
            status=job.status,
            occurred_at=now,
            result=job.result,
            error=job.error,
            reused_from=job.reused_from,
            artifacts_until=job.artifacts_until,
        )


class StageEvent(BaseModel):
    """阶段事件：每个阶段开始、完成、等待重试、失败、待核查或跳过。"""

    type: Literal["stage"] = "stage"
    event_id: UUID = Field(default_factory=uuid4)
    job_id: str
    run: int
    stage: TaskKind
    segment: int | None = None
    attempt: int = 0
    status: StageStatus
    occurred_at: datetime
    model: str | None = None
    prompt_version: int | None = None
    """None 表示内置提示词。"""
    result: dict | None = None
    error: str | None = None
    retry_at: datetime | None = None

    @classmethod
    def of(cls, job: Job, task: Task, now: datetime, profile: AiProfile | None = None) -> Self:
        status = STAGE_STATUS[task.status]
        return cls(
            job_id=job.external_id,
            run=job.run,
            stage=task.kind,
            segment=task.segment,
            attempt=task.attempts,
            status=status,
            occurred_at=now,
            model=profile.label if profile else None,
            prompt_version=profile.prompt_version if profile else None,
            result=task.result if status == StageStatus.SUCCEEDED else None,
            error=task.error,
            retry_at=task.run_after if status == StageStatus.RETRYING else None,
        )
