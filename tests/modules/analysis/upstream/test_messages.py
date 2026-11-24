from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.factories import video_message
from video_analysis.modules.analysis.job.models import Job, JobKind
from video_analysis.modules.analysis.upstream.messages import (
    AnalyzeVideo,
    JobEvent,
    StageEvent,
    StageStatus,
    SummarizeCreator,
)
from video_analysis.modules.scheduling.task.models import (
    RetryPolicy,
    Task,
    TaskKind,
    WorkerIdentity,
)

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


def test_event_factories_serialize_executor_attempt_and_retry_generation():
    job = Job(JobKind.VIDEO, "j-1", 2, "b-1", "c-1", {})
    task = Task(TaskKind.FETCH, job.id, retry_generation=3)
    task.start(NOW, timedelta(minutes=1))
    worker = WorkerIdentity("worker-a", uuid4())
    assert task.lease_id is not None

    stage_event = StageEvent.of(job, task, NOW, worker=worker)
    job.complete({}, NOW)
    job_event = JobEvent.of(
        job, NOW, worker=worker, attempt_id=task.lease_id, retry_generation=task.retry_generation
    )

    for event in (stage_event, job_event):
        payload = event.model_dump(mode="json")
        assert payload["executor_id"] == worker.worker_id
        assert payload["executor_session_id"] == str(worker.worker_session_id)
        assert payload["attempt_id"] == str(task.lease_id)
        assert payload["retry_generation"] == 3


def test_legacy_events_can_be_read_without_executor_fields():
    job = Job(JobKind.VIDEO, "j-1", 2, "b-1", "c-1", {})
    task = Task(TaskKind.FETCH, job.id)
    task.start(NOW, timedelta(minutes=1))
    executor_fields = {"executor_id", "executor_session_id", "attempt_id", "retry_generation"}

    stage = StageEvent.model_validate(
        StageEvent.of(job, task, NOW).model_dump(exclude=executor_fields)
    )
    accepted = JobEvent.model_validate(JobEvent.of(job, NOW).model_dump(exclude=executor_fields))

    for event in (stage, accepted):
        attribution = (event.executor_id, event.executor_session_id, event.attempt_id)
        assert attribution == (None, None, None)
    assert stage.retry_generation == 0
    assert accepted.retry_generation is None
