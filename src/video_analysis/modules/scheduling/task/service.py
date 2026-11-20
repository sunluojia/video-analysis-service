from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.core.common.clock import utcnow
from video_analysis.modules.analysis.job.models import Job, JobStatus
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.scheduling.task.models import TaskStatus
from video_analysis.modules.scheduling.task.repository import TaskRepository

RETRYABLE = (TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW)
SETTLED = (JobStatus.COMPLETED, JobStatus.CANCELLED)


class TaskService:
    """调度中心的人工干预：补跑或取消。重新分析由调度中心投递新一轮任务。

    加锁顺序与执行流程一致：先阶段任务，后分析任务。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._jobs = JobRepository(session)
        self._tasks = TaskRepository(session)

    async def retry_job(self, job_id: str) -> Job:
        job = await self._jobs.require_external(job_id)
        await self._retry(job)
        return job

    async def retry_batch(self, batch_id: str) -> list[Job]:
        """返回被补跑的任务。"""
        return [job for job in await self._jobs.of_batch(batch_id) if await self._retry(job)]

    async def cancel_job(self, job_id: str) -> Job:
        job = await self._jobs.require_external(job_id)
        await self._cancel(job)
        return job

    async def cancel_batch(self, batch_id: str) -> list[Job]:
        """返回被取消的任务。"""
        return [job for job in await self._jobs.of_batch(batch_id) if await self._cancel(job)]

    async def _retry(self, job: Job) -> bool:
        """从失败或待核查的阶段继续，已完成的阶段不重做。"""
        stuck = await self._tasks.of_job(job.id, statuses=RETRYABLE, lock=True)
        await self._jobs.require(Job, job.id, lock=True)
        now = utcnow()
        for task in stuck:
            task.retry(now)
        if stuck:
            job.reopen()
        return bool(stuck)

    async def _cancel(self, job: Job) -> bool:
        """不再推进，派生文件随后清理；已完成和已取消的任务不变。在途请求的结果会被丢弃。"""
        tasks = await self._tasks.of_job(job.id, lock=True)
        await self._jobs.require(Job, job.id, lock=True)
        if job.status in SETTLED:
            return False
        for task in tasks:
            await self._tasks.delete(task)
        job.cancel(utcnow())
        return True
