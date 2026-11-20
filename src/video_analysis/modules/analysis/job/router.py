from pathlib import Path
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import FileResponse

from video_analysis.core.common.errors import NotFound
from video_analysis.core.database.engine import Database
from video_analysis.core.web.dependencies import DatabaseDep, WorkspaceDep
from video_analysis.modules.ai.call.repository import CallRepository
from video_analysis.modules.ai.call.schemas import AiCallView
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.job.schemas import JobDetail, JobView
from video_analysis.modules.scheduling.task.repository import TaskRepository
from video_analysis.modules.scheduling.task.schemas import TaskView

router = APIRouter(prefix="/jobs", tags=["分析任务"])


@router.get("/{job_id}")
async def get_job(job_id: str, db: DatabaseDep) -> JobDetail:
    async with db.transaction() as session:
        job = await JobRepository(session).require_external(job_id)
        tasks = await TaskRepository(session).of_job(job.id)
        calls = await CallRepository(session).of_job(job.id)
    return JobDetail(
        job=JobView.model_validate(job),
        tasks=[TaskView.model_validate(task) for task in tasks],
        ai_calls=[AiCallView.model_validate(call) for call in calls],
    )


@router.get("/{job_id}/frames/{index}")
async def get_frame(
    job_id: str, index: int, db: DatabaseDep, workspace: WorkspaceDep
) -> FileResponse:
    return _file(workspace.frame(await _key(db, job_id), index))


@router.get("/{job_id}/audio/{index}")
async def get_audio(
    job_id: str, index: int, db: DatabaseDep, workspace: WorkspaceDep
) -> FileResponse:
    return _file(workspace.segment(await _key(db, job_id), index))


async def _key(db: Database, job_id: str) -> UUID:
    async with db.transaction() as session:
        return (await JobRepository(session).require_external(job_id)).id


def _file(path: Path) -> FileResponse:
    """抽帧图片和音频段在保留期内可以获取。"""
    if not path.is_file():
        raise NotFound("文件不存在或已过保留期")
    return FileResponse(path)
