from dataclasses import asdict
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, insert, select, update

from video_analysis.core.database.repository import Repository
from video_analysis.modules.ai.call.models import AiProfile, CallOutcome, Usage
from video_analysis.modules.ai.call.tables import ai_calls
from video_analysis.modules.ai.prompt.tables import ai_prompts
from video_analysis.modules.analysis.job.tables import jobs
from video_analysis.modules.scheduling.task.models import Task

type Bucket = Literal["hour", "day"]
TIME_ZONE = "Asia/Shanghai"
"""按北京时间的整点、零点分时段。"""


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
        self,
        claimed: Task,
        outcome: CallOutcome,
        now: datetime,
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        """结束本次领取发出的那一次调用；已按丢失处理的调用不再改写。"""
        await self._session.execute(
            update(ai_calls)
            .where(ai_calls.c.id == claimed.lease_id, ai_calls.c.finished_at.is_(None))
            .values(
                finished_at=now, outcome=outcome, error=error, **(asdict(usage) if usage else {})
            )
        )

    async def expire(self, now: datetime) -> None:
        """超过期限仍未结束的调用视为丢失，释放额度。"""
        await self._session.execute(
            update(ai_calls)
            .where(ai_calls.c.finished_at.is_(None), ai_calls.c.deadline < now)
            .values(finished_at=now, outcome=CallOutcome.LOST)
        )

    async def usage(
        self, since: datetime, until: datetime, bucket: Bucket, batch_id: str | None
    ) -> list[dict]:
        """按开始时间分时段，汇总各类任务、各模型的调用次数、结果、token 和占用时长。"""
        call = ai_calls.c
        at = func.date_trunc(bucket, call.started_at, TIME_ZONE).label("at")
        succeeded = call.outcome == CallOutcome.SUCCEEDED
        ended = func.coalesce(call.finished_at, func.now())

        def total(column, *where):
            values = func.sum(column)
            return func.coalesce(values.filter(*where) if where else values, 0)

        stmt = (
            select(
                at,
                call.kind,
                call.model_id,
                func.max(call.model).label("model"),
                func.count().label("calls"),
                func.count().filter(succeeded).label("succeeded"),
                func.count().filter(call.outcome.is_not(None), ~succeeded).label("failed"),
                total(call.input_tokens).label("input_tokens"),
                total(call.output_tokens).label("output_tokens"),
                total(call.cached_tokens).label("cached_tokens"),
                total(call.input_tokens, call.cached_tokens.is_not(None)).label(
                    "cache_input_tokens"
                ),
                total(func.extract("epoch", ended - call.started_at)).label("busy_seconds"),
            )
            .where(call.started_at >= since, call.started_at < until)
            .group_by(at, call.kind, call.model_id)
            .order_by(at, call.kind)
        )
        if batch_id is not None:
            stmt = stmt.join(jobs, jobs.c.id == call.job_id).where(jobs.c.batch_id == batch_id)
        return [dict(row) for row in (await self._session.execute(stmt)).mappings()]

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
                ai_calls.c.input_tokens,
                ai_calls.c.output_tokens,
                ai_calls.c.cached_tokens,
            )
            .outerjoin(ai_prompts, ai_prompts.c.id == ai_calls.c.prompt_id)
            .where(ai_calls.c.job_id == job_id)
            .order_by(ai_calls.c.started_at)
        )
        return [dict(row) for row in (await self._session.execute(stmt)).mappings()]
