from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from video_analysis.core.common.clock import utcnow
from video_analysis.modules.scheduling.task.models import TaskKind


@dataclass
class SystemPrompt:
    """系统提示词的一个版本。内容不可修改：调整时新建版本，可随时切换或回滚。"""

    kind: TaskKind
    version: int
    content: str
    note: str = ""
    active: bool = False
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utcnow)


def activate(prompts: Sequence[SystemPrompt], chosen: SystemPrompt | None) -> None:
    """同一类任务只有一个生效版本；chosen 为空表示恢复内置提示词。"""
    for prompt in prompts:
        prompt.active = prompt is chosen
