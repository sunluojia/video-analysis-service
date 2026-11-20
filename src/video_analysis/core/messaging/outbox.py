"""发件箱：待回传的事件与业务数据在同一事务写入，提交后由 Worker 投递。"""

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, Identity, Index, Table, insert, select, text, update
from sqlalchemy.dialects.postgresql import JSONB

from video_analysis.core.common.clock import utcnow
from video_analysis.core.database.mapping import metadata, timestamp
from video_analysis.core.database.repository import Repository

outbox = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", timestamp(), nullable=False),
    Column("published_at", timestamp()),
    Index("ix_outbox_pending", "id", postgresql_where=text("published_at IS NULL")),
)


class Outbox(Repository):
    async def put(self, message: BaseModel) -> None:
        await self._session.execute(
            insert(outbox).values(payload=message.model_dump(mode="json"), created_at=utcnow())
        )

    async def pending(self, limit: int) -> list[tuple[int, dict]]:
        """按写入顺序取出待投递的消息；其他 Worker 正在投递的行会被跳过。"""
        stmt = (
            select(outbox.c.id, outbox.c.payload)
            .where(outbox.c.published_at.is_(None))
            .order_by(outbox.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(stmt)).all())

    async def mark_published(self, message_ids: Sequence[int], now: datetime) -> None:
        await self._session.execute(
            update(outbox).where(outbox.c.id.in_(message_ids)).values(published_at=now)
        )
