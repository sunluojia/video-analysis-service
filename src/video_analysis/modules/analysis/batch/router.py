from fastapi import APIRouter

from video_analysis.core.web.dependencies import DatabaseDep
from video_analysis.modules.analysis.batch.repository import BatchRepository

router = APIRouter(prefix="/batches", tags=["批次"])


@router.post("/{batch_id}/pause", status_code=204)
async def pause_batch(batch_id: str, db: DatabaseDep) -> None:
    async with db.transaction() as session:
        await BatchRepository(session).set_paused(batch_id, True)


@router.post("/{batch_id}/resume", status_code=204)
async def resume_batch(batch_id: str, db: DatabaseDep) -> None:
    async with db.transaction() as session:
        await BatchRepository(session).set_paused(batch_id, False)
