from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from video_analysis.modules.scheduling.task.models import TaskKind

PRIORITY = {TaskKind.LISTEN: 0, TaskKind.RECOGNIZE: 1, TaskKind.SUMMARIZE: 2}
AGING_STEP = timedelta(minutes=5)
"""每多等待一个步长，优先级提高一级，避免普通听音长期得不到借用额度。"""


@dataclass(frozen=True)
class AiUsage:
    """某个模型当前的额度占用，由所有 Worker 共同统计。"""

    running: Mapping[TaskKind, int]
    recent_requests: int
    """最近一分钟发出的请求数。"""
    waiting_since: Mapping[TaskKind, datetime]
    """各类可执行任务中等待最久者的可执行时间。"""


@dataclass(frozen=True)
class AiPool:
    """一个模型的请求额度，以及路由到该模型的各类任务的目标份额。"""

    model_id: UUID
    capacity: int
    requests_per_minute: int
    shares: Mapping[TaskKind, int]

    def choose(self, usage: AiUsage, now: datetime) -> TaskKind | None:
        """目标份额＋空闲借用。

        份额未用满且有待执行任务的类型优先；都已用满时，剩余额度借给等待中的类型。
        已发出的请求不中断，后到的类型通过后续释放的额度拿回份额。
        """
        if (
            usage.recent_requests >= self.requests_per_minute
            or sum(usage.running.values()) >= self.capacity
        ):
            return None
        waiting = [kind for kind in usage.waiting_since if kind in self.shares]
        entitled = [kind for kind in waiting if usage.running.get(kind, 0) < self.shares[kind]]
        candidates = entitled or waiting
        if not candidates:
            return None
        return max(candidates, key=lambda kind: self._priority(kind, usage, now))

    @staticmethod
    def _priority(kind: TaskKind, usage: AiUsage, now: datetime) -> float:
        return PRIORITY[kind] + (now - usage.waiting_since[kind]) / AGING_STEP
