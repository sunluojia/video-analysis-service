from uuid import UUID

from pydantic import BaseModel

from video_analysis.modules.analysis.job.models import JobKind, JobStatus
from video_analysis.modules.scheduling.task.models import TaskKind


class ModelUsage(BaseModel):
    model_id: UUID
    capacity: int
    requests_per_minute: int
    running: int
    """在途请求数，所有 Worker 合计。"""
    recent_requests: int
    """最近一分钟发出的请求数。"""


class ServiceStatus(BaseModel):
    instance_id: str
    version: str
    jobs: dict[JobKind, dict[JobStatus, int]]
    ready: dict[TaskKind, int]
    """可以执行、等待领取的阶段任务数。"""
    running: dict[TaskKind, int]
    models: list[ModelUsage]
    free_disk_bytes: int
    """工作目录所在磁盘的剩余空间。"""
