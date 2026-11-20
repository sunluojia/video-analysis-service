from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.core.common.clock import utcnow
from video_analysis.core.messaging.outbox import Outbox
from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.analysis.batch.repository import BatchRepository
from video_analysis.modules.analysis.job.models import Job, JobKind, SummaryInput, VideoInput
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.upstream.messages import (
    AnalyzeVideo,
    JobEvent,
    JobMessage,
    StageEvent,
    StageStatus,
    SummarizeCreator,
)
from video_analysis.modules.scheduling.task.models import Task, TaskKind
from video_analysis.modules.scheduling.task.repository import TaskRepository


class UpstreamService:
    """受理调度中心的任务，并把进度和结果写入发件箱，与业务数据在同一事务提交。

    加锁顺序与执行流程一致：阶段任务、分析任务、批次。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._jobs = JobRepository(session)
        self._tasks = TaskRepository(session)
        self._batches = BatchRepository(session)
        self._outbox = Outbox(session)

    async def accept_video(self, message: AnalyzeVideo) -> None:
        await self._accept(message, JobKind.VIDEO, VideoInput, TaskKind.FETCH)

    async def accept_summary(self, message: SummarizeCreator) -> None:
        await self._accept(message, JobKind.SUMMARY, SummaryInput, TaskKind.SUMMARIZE)

    async def report_job(self, job: Job, now: datetime) -> None:
        await self._outbox.put(JobEvent.of(job, now))

    async def report_task(
        self, job: Job, task: Task, now: datetime, profile: AiProfile | None = None
    ) -> None:
        await self._outbox.put(StageEvent.of(job, task, now, profile))

    async def report_skipped(self, job: Job, stage: TaskKind, reason: str, now: datetime) -> None:
        event = StageEvent(
            job_id=job.external_id,
            run=job.run,
            stage=stage,
            status=StageStatus.SKIPPED,
            occurred_at=now,
            result={"skip_reason": reason},
        )
        await self._outbox.put(event)

    async def _accept(
        self, message: JobMessage, kind: JobKind, inputs: type[BaseModel], first: TaskKind
    ) -> None:
        """登记任务并回传受理。重复投递和旧轮次不重复受理；新轮次丢弃上一轮的进度重新开始。

        已取消的批次不再受理，包括取消前已经进入队列的任务。
        """
        now = utcnow()
        values = message.model_dump(mode="json", include=set(inputs.model_fields))
        job = await self._jobs.by_external_id(message.job_id)
        previous = []
        if job is not None:
            previous = await self._tasks.of_job(job.id, lock=True)
            job = await self._jobs.require(Job, job.id, lock=True)
            if message.run <= job.run:
                return
        if not await self._batches.accepting(message.batch_id):
            return
        if job is None:
            job = Job(
                kind,
                message.job_id,
                message.run,
                message.batch_id,
                message.creator_id,
                values,
                accepted_at=now,
            )
            self._jobs.add(job)
        else:
            for task in previous:
                await self._tasks.delete(task)
            job.restart(message.run, values, now)
        self._tasks.add(Task(first, job.id))
        await self.report_job(job, now)
