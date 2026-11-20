from fastapi import APIRouter, Request

from video_analysis.core.common.clock import utcnow
from video_analysis.core.web.dependencies import DatabaseDep, WorkspaceDep
from video_analysis.modules.ai.quota.repository import QuotaRepository
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.scheduling.status.schemas import ModelUsage, ServiceStatus
from video_analysis.modules.scheduling.task.repository import TaskRepository

router = APIRouter(tags=["运行状态"])


@router.get("/status")
async def get_status(request: Request, db: DatabaseDep, workspace: WorkspaceDep) -> ServiceStatus:
    """任务进度、等待和在途的阶段、各模型额度的占用，以及剩余磁盘。"""
    settings = request.app.state.settings
    async with db.transaction() as session:
        quota, tasks = QuotaRepository(session), TaskRepository(session)
        now = utcnow()
        models = [
            ModelUsage(
                model_id=pool.model_id,
                capacity=pool.capacity,
                requests_per_minute=pool.requests_per_minute,
                running=sum((await quota.running(pool)).values()),
                recent_requests=await quota.recent_requests(pool, now),
            )
            for pool in await quota.pools()
        ]
        return ServiceStatus(
            instance_id=settings.instance_id,
            version=request.app.version,
            jobs=await JobRepository(session).status_counts(),
            ready=await tasks.ready_counts(now, settings.listen_per_video),
            running=await tasks.running_counts(),
            models=models,
            free_disk_bytes=workspace.free_bytes(),
        )
