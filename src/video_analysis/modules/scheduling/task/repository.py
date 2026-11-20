from collections.abc import Collection, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Select, func, or_, select, update

from video_analysis.core.database.repository import Repository
from video_analysis.modules.analysis.batch.tables import batches
from video_analysis.modules.analysis.job.models import JobKind, JobStatus
from video_analysis.modules.analysis.job.results import Listening
from video_analysis.modules.analysis.job.tables import jobs
from video_analysis.modules.scheduling.task.models import AI_KINDS, Task, TaskKind, TaskStatus
from video_analysis.modules.scheduling.task.tables import tasks

FAIR_ORDER = (batches.c.last_dispatched_at.asc().nulls_first(), tasks.c.run_after)
"""批次之间轮流派发，最久没有派发过的批次优先；同一批次按到达先后。"""


class TaskRepository(Repository):
    async def of_job(
        self, job_id: UUID, *, statuses: Sequence[TaskStatus] = (), lock: bool = False
    ) -> list[Task]:
        stmt = select(Task).filter_by(job_id=job_id).order_by(tasks.c.created_at, tasks.c.segment)
        if statuses:
            stmt = stmt.where(tasks.c.status.in_(statuses))
        if lock:
            stmt = stmt.with_for_update()
        return list(await self._session.scalars(stmt))

    async def listen_results(self, job_id: UUID) -> list[Listening]:
        """按时间顺序返回已完成的听音结果。"""
        stmt = (
            select(Task)
            .filter_by(job_id=job_id, kind=TaskKind.LISTEN, status=TaskStatus.SUCCEEDED)
            .order_by(tasks.c.segment)
        )
        return [Listening.model_validate(task.result) for task in await self._session.scalars(stmt)]

    async def waiting_since(
        self, kinds: Collection[TaskKind], now: datetime, listen_limit: int
    ) -> dict[TaskKind, datetime]:
        """各类可执行任务中等待最久者的可执行时间。"""
        stmt = (
            self._executable(select(tasks.c.kind, func.min(tasks.c.run_after)), now, listen_limit)
            .where(tasks.c.kind.in_(kinds))
            .group_by(tasks.c.kind)
        )
        return dict((await self._session.execute(stmt)).all())

    async def ready_counts(self, now: datetime, listen_limit: int) -> dict[TaskKind, int]:
        """各类可执行、尚未领取的任务数。"""
        stmt = self._executable(select(tasks.c.kind, func.count()), now, listen_limit).group_by(
            tasks.c.kind
        )
        return dict((await self._session.execute(stmt)).all())

    async def preparing_videos(self) -> int:
        """处理中、还没有产生 AI 任务的视频数：它们即将带来模型工作。"""
        has_ai_task = (
            select(tasks.c.id)
            .where(tasks.c.job_id == jobs.c.id, tasks.c.kind.in_(AI_KINDS))
            .exists()
        )
        stmt = select(func.count()).where(
            jobs.c.kind == JobKind.VIDEO, jobs.c.status == JobStatus.PROCESSING, ~has_ai_task
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def running_counts(self) -> dict[TaskKind, int]:
        stmt = (
            select(tasks.c.kind, func.count())
            .where(tasks.c.status == TaskStatus.RUNNING)
            .group_by(tasks.c.kind)
        )
        return dict((await self._session.execute(stmt)).all())

    async def next_task(
        self, kinds: Collection[TaskKind], now: datetime, listen_limit: int
    ) -> Task | None:
        """领取一项可执行任务；其他 Worker 正在领取的行会被跳过。"""
        stmt = (
            self._executable(select(Task), now, listen_limit)
            .where(tasks.c.kind.in_(kinds))
            .order_by(*FAIR_ORDER)
            .limit(1)
            .with_for_update(of=tasks, skip_locked=True)
        )
        return await self._session.scalar(stmt)

    async def mark_dispatched(self, task: Task, now: datetime) -> None:
        batch_id = select(jobs.c.batch_id).where(jobs.c.id == task.job_id).scalar_subquery()
        await self._session.execute(
            update(batches).where(batches.c.id == batch_id).values(last_dispatched_at=now)
        )

    async def expired(self, now: datetime) -> list[Task]:
        stmt = (
            select(Task)
            .where(tasks.c.status == TaskStatus.RUNNING, tasks.c.lease_expires_at < now)
            .with_for_update(skip_locked=True)
        )
        return list(await self._session.scalars(stmt))

    @classmethod
    def _executable(cls, stmt: Select, now: datetime, listen_limit: int) -> Select:
        """执行时间已到、批次未暂停、所属任务未失败的就绪任务。任务只在前置条件满足后创建。"""
        return (
            stmt.join(jobs, jobs.c.id == tasks.c.job_id)
            .join(batches, batches.c.id == jobs.c.batch_id)
            .where(
                tasks.c.status == TaskStatus.READY,
                tasks.c.run_after <= now,
                batches.c.paused.is_(False),
                jobs.c.status != JobStatus.FAILED,
                cls._listen_capacity(listen_limit),
            )
        )

    @staticmethod
    def _listen_capacity(limit: int) -> ColumnElement[bool]:
        """单视频同时听音的数量受限，完成一段再补入下一段。"""
        busy = tasks.alias("busy")
        running = (
            select(func.count())
            .select_from(busy)
            .where(
                busy.c.job_id == tasks.c.job_id,
                busy.c.kind == TaskKind.LISTEN,
                busy.c.status == TaskStatus.RUNNING,
            )
            .scalar_subquery()
        )
        return or_(tasks.c.kind != TaskKind.LISTEN, running < limit)
