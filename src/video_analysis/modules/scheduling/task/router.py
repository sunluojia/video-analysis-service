from fastapi import APIRouter

from video_analysis.core.web.dependencies import DatabaseDep
from video_analysis.modules.analysis.batch.repository import BatchRepository
from video_analysis.modules.analysis.job.schemas import JobState
from video_analysis.modules.scheduling.task.service import TaskService

router = APIRouter(tags=["任务控制"])

# 返回的状态都在事务提交前生成，occurred_at 早于此后执行产生的任何事件。


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, db: DatabaseDep) -> JobState:
    async with db.transaction() as session:
        job = await TaskService(session).retry_job(job_id)
        return JobState.model_validate(job)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, db: DatabaseDep) -> JobState:
    async with db.transaction() as session:
        job = await TaskService(session).cancel_job(job_id)
        return JobState.model_validate(job)


@router.post("/batches/{batch_id}/retry")
async def retry_batch(batch_id: str, db: DatabaseDep) -> list[JobState]:
    async with db.transaction() as session:
        jobs = await TaskService(session).retry_batch(batch_id)
        return [JobState.model_validate(job) for job in jobs]


@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str, db: DatabaseDep) -> list[JobState]:
    # 先单独提交取消标记：此后到达的任务不再受理，正在受理的提交之后才轮到这里，下一步一并取消。
    # 不与取消任务放在同一事务，是为了保持阶段任务、分析任务、批次的加锁顺序。
    async with db.transaction() as session:
        await BatchRepository(session).cancel(batch_id)
    async with db.transaction() as session:
        jobs = await TaskService(session).cancel_batch(batch_id)
        return [JobState.model_validate(job) for job in jobs]
