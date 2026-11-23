from datetime import datetime

from fastapi import APIRouter

from video_analysis.core.web.dependencies import DatabaseDep
from video_analysis.modules.ai.call.repository import Bucket, CallRepository
from video_analysis.modules.ai.call.schemas import UsageRow

router = APIRouter(prefix="/ai", tags=["AI 用量"])


@router.get("/usage")
async def usage(
    db: DatabaseDep,
    since: datetime,
    until: datetime,
    bucket: Bucket = "hour",
    batch_id: str | None = None,
) -> list[UsageRow]:
    """时间段内的模型调用，按开始时间分时段、任务类型和模型汇总；可以只看一个批次。"""
    async with db.transaction() as session:
        rows = await CallRepository(session).usage(since, until, bucket, batch_id)
    return [UsageRow.model_validate(row) for row in rows]
