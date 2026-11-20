from datetime import datetime

from video_analysis.core.web.schemas import View
from video_analysis.modules.scheduling.task.models import TaskKind, TaskStatus


class TaskView(View):
    kind: TaskKind
    segment: int | None
    status: TaskStatus
    attempts: int
    result: dict | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
