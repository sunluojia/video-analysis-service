"""按阶段调度：持续领取可执行的任务，完成一项就立即寻找下一项。

下载和媒体作业受本进程名额限制；AI 请求受各模型的全局在途额度和速率限制。
新视频按需开始：等待模型的任务不够多时才下载，让模型额度持续用满而磁盘上不堆积素材。
等待前置结果、额度或重试时间的任务只是留在数据库里，不占用协程、线程或连接。
"""

import asyncio
import logging
from collections.abc import Collection
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.core.common.clock import utcnow
from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace
from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.ai.call.repository import CallRepository
from video_analysis.modules.ai.call.service import CallService
from video_analysis.modules.ai.quota.models import AiPool, AiUsage
from video_analysis.modules.ai.quota.repository import QuotaRepository
from video_analysis.modules.analysis.job.models import Job, JobStatus
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.upstream.service import UpstreamService
from video_analysis.modules.scheduling.execution.pipeline import Pipeline
from video_analysis.modules.scheduling.task.models import AI_KINDS, MEDIA_KINDS, Task, TaskKind
from video_analysis.modules.scheduling.task.repository import TaskRepository

log = logging.getLogger(__name__)

LEASE_GRACE = timedelta(minutes=1)
FETCH_KINDS = frozenset({TaskKind.FETCH})


@dataclass(frozen=True)
class WorkerLimits:
    download_slots: int
    """本进程同时下载的视频数。"""
    media_slots: int
    """本进程同时运行的 FFmpeg 作业数。"""
    admission_factor: float
    """等待模型的任务数低于模型并发额度之和的这个倍数时，开始新视频。"""
    media_backlog: int
    """全局处理中视频数的上限，只用来保护磁盘。"""
    min_free_bytes: int
    listen_per_video: int
    media_timeout: timedelta
    poll_interval: float


class Worker:
    def __init__(
        self, database: Database, pipeline: Pipeline, workspace: Workspace, limits: WorkerLimits
    ) -> None:
        self._database = database
        self._pipeline = pipeline
        self._workspace = workspace
        self._limits = limits
        self._jobs: dict[asyncio.Task, TaskKind] = {}
        self._wake = asyncio.Event()
        self._stopping = False

    async def run(self) -> None:
        while not self._stopping:
            try:
                await self._pipeline.recover_expired()
                await self._pipeline.discard_expired()
                await self._dispatch()
            except Exception as error:
                log.error("调度失败，稍后重试", exc_info=error)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._limits.poll_interval)
            self._wake.clear()
        await asyncio.gather(*self._jobs, return_exceptions=True)

    def stop(self) -> None:
        """停止领取新任务，等已开始的任务结束后退出。"""
        self._stopping = True
        self._wake.set()

    async def _dispatch(self) -> None:
        async with self._database.transaction() as session:
            pools = await QuotaRepository(session).pools()
        for pool in pools:
            while claim := await self._claim_ai(pool):
                self._start(*claim)
        while self._running(MEDIA_KINDS) < self._limits.media_slots and (
            task := await self._claim_media()
        ):
            self._start(task)
        while self._running(FETCH_KINDS) < self._limits.download_slots and (
            task := await self._claim_fetch(pools)
        ):
            self._start(task)

    async def _claim_fetch(self, pools: Collection[AiPool]) -> Task | None:
        """开始一个新视频。准入判断在全局锁内进行，多个 Worker 依次计数，不会同时越过上限。"""
        async with self._database.transaction() as session:
            await JobRepository(session).lock_admission()
            now = utcnow()
            if not await self._admits(session, pools, now):
                return None
            task = await TaskRepository(session).next_task(
                FETCH_KINDS, now, self._limits.listen_per_video
            )
            if task:
                await self._begin(session, task, now, self._limits.media_timeout)
            return task

    async def _admits(
        self, session: AsyncSession, pools: Collection[AiPool], now: datetime
    ) -> bool:
        """等待模型的工作不足以填满额度时备料；处理中视频数和剩余磁盘只作保护。

        等待模型的工作包括可执行的 AI 任务，以及正在下载和抽取、即将产生 AI 任务的视频。
        """
        tasks = TaskRepository(session)
        ready = await tasks.ready_counts(now, self._limits.listen_per_video)
        waiting = sum(ready.get(kind, 0) for kind in AI_KINDS) + await tasks.preparing_videos()
        capacity = sum(pool.capacity for pool in pools)
        return (
            waiting < self._limits.admission_factor * capacity
            and await JobRepository(session).processing_videos() < self._limits.media_backlog
            and self._workspace.free_bytes() >= self._limits.min_free_bytes
        )

    async def _claim_media(self) -> Task | None:
        async with self._database.transaction() as session:
            now = utcnow()
            task = await TaskRepository(session).next_task(
                MEDIA_KINDS, now, self._limits.listen_per_video
            )
            if task:
                await self._begin(session, task, now, self._limits.media_timeout)
            return task

    async def _claim_ai(self, pool: AiPool) -> tuple[Task, AiProfile] | None:
        """锁住模型额度后按目标份额选择任务类型，多个 Worker 的领取决定依次进行。

        本次请求的模型、提示词和超时在领取时确定，之后切换路由不影响已领取的任务。
        """
        async with self._database.transaction() as session:
            quota, tasks = QuotaRepository(session), TaskRepository(session)
            now = utcnow()
            listen_limit = self._limits.listen_per_video
            await quota.lock(pool)
            usage = AiUsage(
                running=await quota.running(pool),
                recent_requests=await quota.recent_requests(pool, now),
                waiting_since=await tasks.waiting_since(pool.shares.keys(), now, listen_limit),
            )
            kind = pool.choose(usage, now)
            if kind is None:
                return None
            task = await tasks.next_task([kind], now, listen_limit)
            if task is None:
                return None
            profile = await CallService(session).profile(pool.model_id, kind)
            timeout = timedelta(seconds=profile.timeout_seconds)
            await self._begin(session, task, now, timeout, profile)
            await CallRepository(session).start(task, profile)
            return task, profile

    @staticmethod
    async def _begin(
        session: AsyncSession,
        task: Task,
        now: datetime,
        timeout: timedelta,
        profile: AiProfile | None = None,
    ) -> None:
        """领取任务并回传阶段开始；任务的第一个阶段开始时进入处理中。加锁顺序：阶段任务、分析任务、批次。"""
        task.start(now, timeout + LEASE_GRACE)
        job = await JobRepository(session).require(Job, task.job_id, lock=True)
        if job.status == JobStatus.ACCEPTED:
            job.start()
        await TaskRepository(session).mark_dispatched(task, now)
        await UpstreamService(session).report_task(job, task, now, profile)

    def _start(self, task: Task, profile: AiProfile | None = None) -> None:
        job = asyncio.create_task(self._pipeline.run(task, profile))
        self._jobs[job] = task.kind
        job.add_done_callback(self._finished)

    def _finished(self, job: asyncio.Task) -> None:
        del self._jobs[job]
        if not job.cancelled() and job.exception():
            log.error("保存任务结果失败，租约到期后回收", exc_info=job.exception())
        self._wake.set()

    def _running(self, kinds: Collection[TaskKind]) -> int:
        return sum(kind in kinds for kind in self._jobs.values())
