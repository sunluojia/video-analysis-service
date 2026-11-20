from datetime import UTC, datetime, timedelta
from uuid import uuid4

from video_analysis.modules.scheduling.task.models import RetryPolicy, Task, TaskKind, TaskStatus

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def test_failed_task_backs_off_then_stops():
    task = Task(TaskKind.FETCH, uuid4())
    retry = RetryPolicy(max_attempts=2, base_delay=timedelta(seconds=10))
    task.start(NOW, timedelta(minutes=1))
    task.fail("boom", NOW, retry)
    assert (task.status, task.run_after) == (TaskStatus.READY, NOW + timedelta(seconds=10))
    task.start(NOW, timedelta(minutes=1))
    task.fail("boom", NOW, retry)
    assert task.status == TaskStatus.FAILED


def test_review_and_retry():
    task = Task(TaskKind.RECOGNIZE, uuid4())
    task.start(NOW, timedelta(minutes=1))
    task.hold_for_review("超时", NOW)
    assert task.status == TaskStatus.NEEDS_REVIEW
    task.retry(NOW)
    assert (task.status, task.attempts, task.error) == (TaskStatus.READY, 0, None)
