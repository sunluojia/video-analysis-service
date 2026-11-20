from datetime import datetime

from pydantic import Field

from video_analysis.core.common.clock import utcnow
from video_analysis.core.web.schemas import View
from video_analysis.modules.ai.call.schemas import AiCallView
from video_analysis.modules.analysis.job.models import JobKind, JobStatus
from video_analysis.modules.scheduling.task.schemas import TaskView


class JobState(View):
    """按调度中心的任务 ID 标识。"""

    job_id: str = Field(validation_alias="external_id")
    run: int
    kind: JobKind
    status: JobStatus
    error: str | None
    reused_from: str | None
    accepted_at: datetime
    finished_at: datetime | None
    artifacts_until: datetime | None
    occurred_at: datetime = Field(default_factory=utcnow)
    """取得这个状态的时间，与任务事件的 occurred_at 同一时钟；调度中心据此判断先后。"""


class JobView(JobState):
    batch_id: str
    creator_id: str
    duration: float | None
    frame_count: int | None
    segment_count: int | None
    result: dict | None


class JobDetail(View):
    job: JobView
    tasks: list[TaskView]
    """当前轮次的各阶段。"""
    ai_calls: list[AiCallView]
    """包括之前轮次的模型调用。"""
