from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.ai.config.models import AiModel, AiProvider, AiRoute
from video_analysis.modules.ai.config.repository import AiConfigRepository
from video_analysis.modules.ai.prompt.builtin import BUILTIN_PROMPTS
from video_analysis.modules.ai.prompt.repository import PromptRepository
from video_analysis.modules.scheduling.task.models import TaskKind


class CallService:
    def __init__(self, session: AsyncSession) -> None:
        self._config = AiConfigRepository(session)
        self._prompts = PromptRepository(session)

    async def profile(self, model_id: UUID, kind: TaskKind) -> AiProfile:
        """该模型当前的配置，以及这类任务生效的提示词。"""
        model = await self._config.require(AiModel, model_id)
        provider = await self._config.require(AiProvider, model.provider_id)
        prompt = await self._prompts.active(kind)
        return AiProfile(
            model_id=model.id,
            provider=provider.provider,
            model=model.name,
            base_url=provider.base_url,
            credential_ref=provider.credential_ref,
            params=model.params,
            timeout_seconds=model.timeout_seconds,
            prompt=prompt.content if prompt else BUILTIN_PROMPTS[kind],
            prompt_id=prompt.id if prompt else None,
            prompt_version=prompt.version if prompt else None,
        )

    async def routed(self, kind: TaskKind) -> AiProfile | None:
        """按当前路由，这类任务会使用的配置；没有配置路由时为 None。"""
        route = await self._config.get(AiRoute, kind)
        return await self.profile(route.model_id, kind) if route else None
