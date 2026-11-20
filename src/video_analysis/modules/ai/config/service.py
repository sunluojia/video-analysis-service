from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.modules.ai.config.models import AiModel, AiRoute
from video_analysis.modules.ai.config.repository import AiConfigRepository
from video_analysis.modules.scheduling.task.models import TaskKind


class AiConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._config = AiConfigRepository(session)

    async def save_route(self, kind: TaskKind, model_id: UUID, share: int) -> AiRoute:
        """新建或修改路由，只影响之后发出的请求。"""
        await self._config.require(AiModel, model_id)
        route = await self._config.get(AiRoute, kind, lock=True)
        if route is None:
            route = AiRoute(kind, model_id, share)
            self._config.add(route)
        else:
            route.model_id, route.share = model_id, share
        return route
