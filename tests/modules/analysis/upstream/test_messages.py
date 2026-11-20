from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tests.factories import video_message
from video_analysis.modules.analysis.job.models import Job, JobKind
from video_analysis.modules.analysis.upstream.messages import (
    AnalyzeVideo,
    StageEvent,
    StageStatus,
    SummarizeCreator,
)
from video_analysis.modules.scheduling.task.models import RetryPolicy, Task, TaskKind

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def test_video_message_requires_a_verifiable_material():
    payload = video_message().model_dump(mode="json")
    payload["material"]["sha256"] = "not-a-digest"

    with pytest.raises(ValidationError):
        AnalyzeVideo.model_validate(payload)


def test_summary_needs_at_least_one_video():
    payload = {"job_id": "s-1", "run": 1, "batch_id": "b-1", "creator_id": "c-1", "videos": []}

    with pytest.raises(ValidationError):
        SummarizeCreator.model_validate(payload)


def test_stage_event_carries_the_result_only_on_success_and_retry_time_only_on_retry():
    job = Job(JobKind.VIDEO, "j-1", 2, "b-1", "c-1", {})
    task = Task(TaskKind.FETCH, job.id)
    task.start(NOW, timedelta(minutes=1))
    task.fail("下载失败", NOW, RetryPolicy(max_attempts=2, base_delay=timedelta(seconds=10)))

    retrying = StageEvent.of(job, task, NOW)
    assert (retrying.status, retrying.retry_at, retrying.result) == (
        StageStatus.RETRYING,
        NOW + timedelta(seconds=10),
        None,
    )

    task.start(NOW, timedelta(minutes=1))
    task.succeed(NOW, {"size": 1})
    succeeded = StageEvent.of(job, task, NOW)
    assert (succeeded.job_id, succeeded.run, succeeded.attempt) == ("j-1", 2, 2)
    assert (succeeded.status, succeeded.result, succeeded.retry_at) == (
        StageStatus.SUCCEEDED,
        {"size": 1},
        None,
    )
