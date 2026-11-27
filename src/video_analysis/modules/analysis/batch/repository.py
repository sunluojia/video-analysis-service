from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from video_analysis.core.database.repository import Repository
from video_analysis.modules.analysis.batch.tables import batches


class BatchRepository(Repository):
    async def accepting(self, batch_id: str, ai_batch_mode: bool | None = None) -> bool:
        """返回批次是否还受理任务；第一次见到的批次登记为未暂停。

        队列消息只在首次登记时初始化模式；模式控制接口优先于排队中的旧消息。
        共享锁持有到提交：取消要等正在受理的任务提交，随后把它们一并取消。
        """
        await self._session.execute(
            insert(batches)
            .values(id=batch_id, paused=False, ai_batch_mode=ai_batch_mode is True)
            .on_conflict_do_nothing()
        )
        stmt = (
            select(batches.c.cancelled).where(batches.c.id == batch_id).with_for_update(read=True)
        )
        return not await self._session.scalar(stmt)

    async def set_ai_batch_mode(self, batch_id: str, enabled: bool) -> None:
        stmt = insert(batches).values(id=batch_id, paused=False, ai_batch_mode=enabled)
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=[batches.c.id], set_={"ai_batch_mode": enabled}
            )
        )

    async def ai_batch_modes(self) -> dict[str, bool]:
        stmt = select(batches.c.id, batches.c.ai_batch_mode).order_by(batches.c.id)
        return dict((await self._session.execute(stmt)).all())

    async def set_paused(self, batch_id: str, paused: bool) -> None:
        """暂停只是不再领取该批次的任务，已发出的请求照常完成。批次可以先于任务被暂停。"""
        stmt = insert(batches).values(id=batch_id, paused=paused)
        await self._session.execute(
            stmt.on_conflict_do_update(index_elements=[batches.c.id], set_={"paused": paused})
        )

    async def cancel(self, batch_id: str) -> None:
        """之后到达的任务不再受理。批次可以先于任务被取消。"""
        stmt = insert(batches).values(id=batch_id, paused=False, cancelled=True)
        await self._session.execute(
            stmt.on_conflict_do_update(index_elements=[batches.c.id], set_={"cancelled": True})
        )
