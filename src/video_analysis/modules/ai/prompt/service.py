from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.core.common.errors import NotFound
from video_analysis.modules.ai.prompt.models import SystemPrompt, activate
from video_analysis.modules.ai.prompt.repository import PromptRepository
from video_analysis.modules.scheduling.task.models import TaskKind


class PromptService:
    def __init__(self, session: AsyncSession) -> None:
        self._prompts = PromptRepository(session)

    async def add_version(
        self, kind: TaskKind, content: str, note: str, *, make_active: bool
    ) -> SystemPrompt:
        """保存为新版本，旧版本保留。"""
        prompts = await self._prompts.of_kind(kind, lock=True)
        version = max((prompt.version for prompt in prompts), default=0) + 1
        prompt = SystemPrompt(kind, version, content, note)
        self._prompts.add(prompt)
        if make_active:
            activate([*prompts, prompt], prompt)
        return prompt

    async def activate(self, kind: TaskKind, prompt_id: UUID | None) -> None:
        """切换到指定版本；prompt_id 为空表示恢复内置提示词。只影响之后发出的请求。"""
        prompts = await self._prompts.of_kind(kind, lock=True)
        chosen = next((prompt for prompt in prompts if prompt.id == prompt_id), None)
        if prompt_id and chosen is None:
            raise NotFound(f"提示词版本 {prompt_id} 不存在")
        activate(prompts, chosen)
