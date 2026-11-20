from sqlalchemy import select

from video_analysis.core.database.repository import Repository
from video_analysis.modules.ai.prompt.models import SystemPrompt
from video_analysis.modules.ai.prompt.tables import ai_prompts
from video_analysis.modules.scheduling.task.models import TaskKind


class PromptRepository(Repository):
    async def of_kind(self, kind: TaskKind, *, lock: bool = False) -> list[SystemPrompt]:
        """按版本从新到旧。"""
        stmt = select(SystemPrompt).filter_by(kind=kind).order_by(ai_prompts.c.version.desc())
        if lock:
            stmt = stmt.with_for_update()
        return list(await self._session.scalars(stmt))

    async def active(self, kind: TaskKind) -> SystemPrompt | None:
        return await self._session.scalar(select(SystemPrompt).filter_by(kind=kind, active=True))
