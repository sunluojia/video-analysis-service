from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.scheduling.task.models import TaskKind

TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})


@dataclass
class BatchJob:
    id: UUID
    provider_id: UUID
    model_id: UUID
    model_name: str
    task_kind: TaskKind
    profile_snapshot: dict
    request_count: int
    created_at: datetime
    updated_at: datetime
    status: str = "submitting"
    remote_file_id: str | None = None
    remote_batch_id: str | None = None
    completed_count: int = 0
    failed_count: int = 0
    output_file_id: str | None = None
    error_file_id: str | None = None
    error: str | None = None
    finished_at: datetime | None = None

    @property
    def profile(self) -> AiProfile:
        values = self.profile_snapshot.copy()
        if values["prompt_id"]:
            values["prompt_id"] = UUID(values["prompt_id"])
        return AiProfile(model_id=self.model_id, model=self.model_name, **values)


@dataclass
class BatchItem:
    custom_id: str
    batch_job_id: UUID
    task_id: UUID
    job_id: UUID
    lease_id: UUID
    model_id: UUID
    prompt_version: int | None
    frame_count: int | None
    line_number: int
    created_at: datetime
    status: str = "running"
    error: str | None = None
    finished_at: datetime | None = None
