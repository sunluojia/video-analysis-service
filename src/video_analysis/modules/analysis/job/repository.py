from datetime import datetime

from sqlalchemy import func, select

from video_analysis.core.common.errors import NotFound
from video_analysis.core.database.repository import Repository
from video_analysis.modules.analysis.job.models import Job, JobKind, JobStatus
from video_analysis.modules.analysis.job.tables import jobs

ADMISSION_LOCK = 41_001
"""“开始新视频”准入判断使用的事务级咨询锁键。"""


class JobRepository(Repository):
    async def lock_admission(self) -> None:
        """多个 Worker 的准入判断依次进行；锁在事务结束时释放。"""
        await self._session.execute(select(func.pg_advisory_xact_lock(ADMISSION_LOCK)))

    async def by_external_id(self, external_id: str) -> Job | None:
        return await self._session.scalar(select(Job).filter_by(external_id=external_id))

    async def require_external(self, external_id: str) -> Job:
        job = await self.by_external_id(external_id)
        if job is None:
            raise NotFound(f"分析任务 {external_id} 不存在")
        return job

    async def of_batch(self, batch_id: str) -> list[Job]:
        stmt = select(Job).filter_by(batch_id=batch_id).order_by(jobs.c.accepted_at)
        return list(await self._session.scalars(stmt))

    async def processing_videos(self) -> int:
        stmt = select(func.count()).where(
            jobs.c.kind == JobKind.VIDEO, jobs.c.status == JobStatus.PROCESSING
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def reusable(self, analysis_key: str | None) -> Job | None:
        """输入和模型配置相同、已经完成的最近一次分析。"""
        if analysis_key is None:
            return None
        stmt = (
            select(Job)
            .filter_by(analysis_key=analysis_key, status=JobStatus.COMPLETED)
            .order_by(jobs.c.finished_at.desc())
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def artifacts_expired(self, now: datetime) -> list[Job]:
        stmt = (
            select(Job)
            .where(jobs.c.artifacts_until < now)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        return list(await self._session.scalars(stmt))

    async def status_counts(self) -> dict[JobKind, dict[JobStatus, int]]:
        stmt = select(jobs.c.kind, jobs.c.status, func.count()).group_by(jobs.c.kind, jobs.c.status)
        counts: dict[JobKind, dict[JobStatus, int]] = {kind: {} for kind in JobKind}
        for kind, status, count in await self._session.execute(stmt):
            counts[kind][status] = count
        return counts
