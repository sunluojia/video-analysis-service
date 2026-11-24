from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import insert, update

from video_analysis.core.database.repository import Repository
from video_analysis.modules.analysis.job.models import Job
from video_analysis.modules.scheduling.task.models import (
    AttemptStatus,
    Task,
    TaskStatus,
    WorkerIdentity,
)
from video_analysis.modules.scheduling.task.tables import analysis_attempts


class AttemptRepository(Repository):
    async def start(self, job: Job, task: Task, worker: WorkerIdentity) -> None:
        await self._session.execute(
            insert(analysis_attempts).values(
                attempt_id=task.lease_id,
                task_id=task.id,
                job_external_id=job.external_id,
                run=job.run,
                retry_generation=task.retry_generation,
                stage=task.kind,
                segment=task.segment,
                attempt_no=task.attempts,
                worker_id=worker.worker_id,
                worker_session_id=worker.worker_session_id,
                started_at=task.started_at,
                status=AttemptStatus.RUNNING,
            )
        )

    async def finish(self, task: Task, now: datetime) -> WorkerIdentity | None:
        """结束当前租约并返回领取者；部署迁移前的租约可能没有历史记录。"""
        status = (
            AttemptStatus.RETRYING
            if task.status == TaskStatus.READY
            else AttemptStatus(task.status)
        )
        columns = analysis_attempts.c
        stmt = (
            update(analysis_attempts)
            .where(columns.attempt_id == task.lease_id, columns.ended_at.is_(None))
            .values(ended_at=now, status=status, error=task.error)
            .returning(columns.worker_id, columns.worker_session_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return WorkerIdentity(*row) if row is not None else None

    async def abandon(self, tasks: Iterable[Task], status: AttemptStatus, now: datetime) -> None:
        """取消或重建任务时结束仍在执行的租约，保留此前已结清的记录。"""
        leases = [task.lease_id for task in tasks if task.status == TaskStatus.RUNNING]
        if leases:
            columns = analysis_attempts.c
            await self._session.execute(
                update(analysis_attempts)
                .where(columns.attempt_id.in_(leases), columns.ended_at.is_(None))
                .values(ended_at=now, status=status)
            )
