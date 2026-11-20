from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.core.common.errors import NotFound


class Repository:
    """各业务模块仓储的公共操作；同一事务内的仓储共用一个会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, entity: object) -> None:
        self._session.add(entity)

    async def get[T](self, entity: type[T], key: Any, *, lock: bool = False) -> T | None:
        """加锁读取时重新载入行，拿到的是加锁之后的最新数据。"""
        return await self._session.get(
            entity, key, with_for_update=lock or None, populate_existing=lock
        )

    async def require[T](self, entity: type[T], key: Any, *, lock: bool = False) -> T:
        found = await self.get(entity, key, lock=lock)
        if found is None:
            raise NotFound(f"{entity.__name__} {key} 不存在")
        return found

    async def delete(self, entity: object) -> None:
        await self._session.delete(entity)

    async def list_all[T](self, entity: type[T]) -> list[T]:
        return list(await self._session.scalars(select(entity)))
