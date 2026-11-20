from fastapi import APIRouter

from video_analysis.core.web.dependencies import DatabaseDep
from video_analysis.modules.ai.config.schemas import AiKind
from video_analysis.modules.ai.prompt.builtin import BUILTIN_PROMPTS
from video_analysis.modules.ai.prompt.repository import PromptRepository
from video_analysis.modules.ai.prompt.schemas import (
    ActivePrompt,
    PromptHistory,
    PromptIn,
    PromptView,
)
from video_analysis.modules.ai.prompt.service import PromptService
from video_analysis.modules.scheduling.task.models import TaskKind

router = APIRouter(prefix="/ai/prompts", tags=["系统提示词"])


@router.get("/{kind}")
async def prompt_history(kind: AiKind, db: DatabaseDep) -> PromptHistory:
    async with db.transaction() as session:
        versions = await PromptRepository(session).of_kind(TaskKind(kind))
    return PromptHistory(
        builtin=BUILTIN_PROMPTS[TaskKind(kind)],
        versions=[PromptView.model_validate(version) for version in versions],
    )


@router.post("/{kind}", status_code=201)
async def add_prompt(kind: AiKind, body: PromptIn, db: DatabaseDep) -> PromptView:
    async with db.transaction() as session:
        prompt = await PromptService(session).add_version(
            TaskKind(kind), body.content, body.note, make_active=body.activate
        )
    return PromptView.model_validate(prompt)


@router.put("/{kind}/active", status_code=204)
async def activate_prompt(kind: AiKind, body: ActivePrompt, db: DatabaseDep) -> None:
    async with db.transaction() as session:
        await PromptService(session).activate(TaskKind(kind), body.prompt_id)
