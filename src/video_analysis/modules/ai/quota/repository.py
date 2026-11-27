from collections import defaultdict
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from video_analysis.core.database.repository import Repository
from video_analysis.modules.ai.batch.eligibility import eligible
from video_analysis.modules.ai.call.tables import ai_calls
from video_analysis.modules.ai.config.models import AiModel, AiProvider, AiRoute
from video_analysis.modules.ai.config.tables import ai_models, ai_providers, ai_routes
from video_analysis.modules.ai.quota.models import AiPool
from video_analysis.modules.scheduling.task.models import TaskKind


class QuotaRepository(Repository):
    async def pools(self) -> list[AiPool]:
        """已配置路由的模型额度。"""
        stmt = (
            select(AiModel, AiRoute, AiProvider)
            .join(AiRoute, ai_routes.c.model_id == ai_models.c.id)
            .join(AiProvider, ai_providers.c.id == ai_models.c.provider_id)
            .order_by(ai_models.c.id)
        )
        models: dict[UUID, AiModel] = {}
        shares: dict[UUID, dict[TaskKind, int]] = defaultdict(dict)
        batch_kinds: dict[UUID, set[TaskKind]] = defaultdict(set)
        for model, route, provider in await self._session.execute(stmt):
            models[model.id] = model
            shares[model.id][route.kind] = route.share
            if not model.params.get("use_responses_api") and eligible(
                route.kind, provider.provider, model.name, provider.base_url
            ):
                batch_kinds[model.id].add(route.kind)
        return [
            AiPool(
                model.id,
                model.max_concurrency,
                model.requests_per_minute,
                shares[model.id],
                frozenset(batch_kinds[model.id]),
            )
            for model in models.values()
        ]

    async def lock(self, pool: AiPool) -> None:
        """锁住模型行，使多个 Worker 对同一额度的领取决定依次进行。"""
        await self._session.execute(
            select(ai_models.c.id).where(ai_models.c.id == pool.model_id).with_for_update()
        )

    async def running(self, pool: AiPool) -> dict[TaskKind, int]:
        """各类任务在途的请求数。"""
        stmt = (
            select(ai_calls.c.kind, func.count())
            .where(
                ai_calls.c.model_id == pool.model_id,
                ai_calls.c.finished_at.is_(None),
                ai_calls.c.ai_batch.is_(False),
            )
            .group_by(ai_calls.c.kind)
        )
        return dict((await self._session.execute(stmt)).all())

    async def recent_requests(self, pool: AiPool, now: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(ai_calls)
            .where(
                ai_calls.c.model_id == pool.model_id,
                ai_calls.c.started_at > now - timedelta(minutes=1),
                ai_calls.c.ai_batch.is_(False),
            )
        )
        return (await self._session.execute(stmt)).scalar_one()
