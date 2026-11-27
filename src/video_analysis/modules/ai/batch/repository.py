from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.sql import ColumnElement

from video_analysis.core.database.repository import Repository
from video_analysis.modules.ai.batch.models import BatchItem, BatchJob
from video_analysis.modules.ai.batch.tables import ai_batch_items, ai_batch_jobs
from video_analysis.modules.analysis.job.tables import jobs


class BatchJobRepository(Repository):
    async def acquire_manager(self) -> bool:
        """One packer/poller coordinates active Batch jobs across workers."""
        return bool(await self._session.scalar(select(func.pg_try_advisory_xact_lock(174816504))))

    async def active(self) -> list[BatchJob]:
        stmt = (
            select(BatchJob)
            .where(ai_batch_jobs.c.finished_at.is_(None))
            .order_by(ai_batch_jobs.c.created_at)
        )
        return list(await self._session.scalars(stmt))

    async def pending_items(self, batch_job_id: UUID) -> list[BatchItem]:
        stmt = select(BatchItem).filter_by(batch_job_id=batch_job_id, finished_at=None)
        return list(await self._session.scalars(stmt))

    async def batch_ids(self, batch_job_id: UUID) -> list[str]:
        stmt = (
            select(jobs.c.batch_id)
            .join(ai_batch_items, ai_batch_items.c.job_id == jobs.c.id)
            .where(ai_batch_items.c.batch_job_id == batch_job_id)
            .distinct()
            .order_by(jobs.c.batch_id)
        )
        return list(await self._session.scalars(stmt))


def active_batch_lease(lease_id: ColumnElement) -> ColumnElement[bool]:
    return (
        select(ai_batch_items.c.lease_id)
        .join(ai_batch_jobs)
        .where(ai_batch_items.c.lease_id == lease_id, ai_batch_jobs.c.finished_at.is_(None))
        .exists()
    )
