from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from video_analysis.core.common.clock import utcnow


class TaskKind(StrEnum):
    FETCH = "fetch"
    """下载视频、校验并读取媒体信息。"""
    EXTRACT_FRAMES = "extract_frames"
    EXTRACT_AUDIO = "extract_audio"
    LISTEN = "listen"
    RECOGNIZE = "recognize"
    SUMMARIZE = "summarize"

    @property
    def uses_ai(self) -> bool:
        return self in AI_KINDS


AI_KINDS = frozenset({TaskKind.LISTEN, TaskKind.RECOGNIZE, TaskKind.SUMMARIZE})
MEDIA_KINDS = frozenset({TaskKind.EXTRACT_FRAMES, TaskKind.EXTRACT_AUDIO})
"""占用 FFmpeg 名额的任务。获取视频只占网络，单独限制下载并发。"""


class TaskStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: timedelta = timedelta(seconds=30)

    def next_run(self, attempts: int, now: datetime) -> datetime | None:
        if attempts >= self.max_attempts:
            return None
        return now + self.base_delay * 2 ** (attempts - 1)


@dataclass
class Task:
    kind: TaskKind
    job_id: UUID
    segment: int | None = None
    id: UUID = field(default_factory=uuid4)
    status: TaskStatus = TaskStatus.READY
    attempts: int = 0
    run_after: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    lease_id: UUID | None = None
    """每次领取生成的新标识；只有仍持有该租约的执行才能保存结果，补跑后旧执行不会被误认。"""
    lease_expires_at: datetime | None = None
    result: dict | None = None
    """该阶段的结构化结果，同时回传调度中心。"""
    error: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    def start(self, now: datetime, lease: timedelta) -> None:
        self.status = TaskStatus.RUNNING
        self.attempts += 1
        self.started_at = now
        self.lease_id = uuid4()
        self.lease_expires_at = now + lease

    def succeed(self, now: datetime, result: dict) -> None:
        self.status = TaskStatus.SUCCEEDED
        self.result = result
        self.error = None
        self._finish(now)

    def fail(self, error: str, now: datetime, retry: RetryPolicy | None) -> None:
        """失败后按退避时间重新排队；不重试或次数用尽则终止。"""
        self.error = error
        self.lease_expires_at = None
        next_run = retry.next_run(self.attempts, now) if retry else None
        if next_run is None:
            self.status = TaskStatus.FAILED
            self._finish(now)
        else:
            self.status = TaskStatus.READY
            self.run_after = next_run

    def hold_for_review(self, error: str, now: datetime) -> None:
        """请求已提交但回执不明：不自动重发，等待人工核查。"""
        self.status = TaskStatus.NEEDS_REVIEW
        self.error = error
        self._finish(now)

    def retry(self, now: datetime) -> None:
        self.status = TaskStatus.READY
        self.attempts = 0
        self.run_after = now
        self.error = None
        self.finished_at = None

    def _finish(self, now: datetime) -> None:
        self.lease_expires_at = None
        self.finished_at = now
