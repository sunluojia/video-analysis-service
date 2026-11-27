from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.ai.call.repository import CallRepository
from video_analysis.modules.analysis.job.models import Job, JobStatus
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.upstream.service import UpstreamService
from video_analysis.modules.scheduling.task.attempts import AttemptRepository
from video_analysis.modules.scheduling.task.models import Task, WorkerIdentity
from video_analysis.modules.scheduling.task.repository import TaskRepository


async def begin_task(
    session: AsyncSession,
    task: Task,
    now: datetime,
    lease: timedelta,
    identity: WorkerIdentity,
    profile: AiProfile | None = None,
    *,
    ai_batch: bool = False,
) -> None:
    """Start a claimed stage. Lock order is task, job, then batch."""
    task.start(now, lease)
    job = await JobRepository(session).require(Job, task.job_id, lock=True)
    if job.status == JobStatus.ACCEPTED:
        job.start()
    await TaskRepository(session).mark_dispatched(task, now)
    await AttemptRepository(session).start(job, task, identity)
    await UpstreamService(session).report_task(job, task, now, profile, worker=identity)
    if profile:
        await CallRepository(session).start(
            task, profile, worker_id=identity.worker_id, ai_batch=ai_batch
        )
