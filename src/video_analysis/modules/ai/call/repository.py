from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, update

from video_analysis.core.database.repository import Repository
from video_analysis.modules.ai.call.models import AiProfile, CallOutcome
from video_analysis.modules.ai.call.tables import ai_calls
from video_analysis.modules.ai.prompt.tables import ai_prompts
from video_analysis.modules.scheduling.task.models import Task


class CallRepository(Repository):
    async def start(self, task: Task, profile: AiProfile) -> None:
        """领取时登记调用，在途请求即刻占用模型额度。调用记录以本次租约为标识，租约到期即调用期限。"""
        await self._session.execute(
            insert(ai_calls).values(
                id=task.lease_id,
                task_id=task.id,
                job_id=task.job_id,
                kind=task.kind,
                model_id=profile.model_id,
                model=profile.label,
                prompt_id=profile.prompt_id,
                started_at=task.started_at,
                deadline=task.lease_expires_at,
            )
        )

    async def finish(
        self, claimed: Task, outcome: CallOutcome, now: datetime, error: str | None = None
    ) -> None:
        """结束本次领取发出的那一次调用；已按丢失处理的调用不再改写。"""
        await self._session.execute(
            update(ai_calls)
            .where(ai_calls.c.id == claimed.lease_id, ai_calls.c.finished_at.is_(None))
            .values(finished_at=now, outcome=outcome, error=error)
        )

    async def expire(self, now: datetime) -> None:
        """超过期限仍未结束的调用视为丢失，释放额度。"""
        await self._session.execute(
            update(ai_calls)
            .where(ai_calls.c.finished_at.is_(None), ai_calls.c.deadline < now)
            .values(finished_at=now, outcome=CallOutcome.LOST)
        )

    async def of_job(self, job_id: UUID) -> list[dict]:
        """任务各阶段的调用，包括之前轮次的，附带所用提示词版本。"""
        stmt = (
            select(
                ai_calls.c.kind,
                ai_calls.c.model,
                ai_prompts.c.version.label("prompt_version"),
                ai_calls.c.started_at,
                ai_calls.c.finished_at,
                ai_calls.c.outcome,
                ai_calls.c.error,
            )
            .outerjoin(ai_prompts, ai_prompts.c.id == ai_calls.c.prompt_id)
            .where(ai_calls.c.job_id == job_id)
            .order_by(ai_calls.c.started_at)
        )
        return [dict(row) for row in (await self._session.execute(stmt)).mappings()]
