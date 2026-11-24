from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from dramatiq import Message
from dramatiq.brokers.stub import StubBroker
from sqlalchemy import select, update

from tests.factories import (
    accept,
    configure_ai,
    emitted,
    events,
    load_job,
    summary_message,
    video_message,
)
from tests.modules.scheduling.execution.fakes import Setup
from video_analysis.core.common.clock import utcnow
from video_analysis.core.messaging.outbox import Outbox
from video_analysis.integrations.mq.publisher import EventPublisher
from video_analysis.modules.ai.call.models import CallOutcome
from video_analysis.modules.ai.call.tables import ai_calls
from video_analysis.modules.ai.quota.repository import QuotaRepository
from video_analysis.modules.analysis.job.models import JobStatus
from video_analysis.modules.scheduling.execution.ports import Rejected
from video_analysis.modules.scheduling.task.models import (
    AttemptStatus,
    RetryPolicy,
    Task,
    TaskKind,
    TaskStatus,
    WorkerIdentity,
)
from video_analysis.modules.scheduling.task.repository import TaskRepository
from video_analysis.modules.scheduling.task.service import TaskService
from video_analysis.modules.scheduling.task.tables import analysis_attempts, tasks

OWNER = WorkerIdentity("worker-owner", uuid4())
OTHER = WorkerIdentity("worker-other", uuid4())


async def configured_pools(database):
    await configure_ai(database)
    async with database.transaction() as session:
        return await QuotaRepository(session).pools()


async def attempt_rows(database):
    async with database.engine.connect() as connection:
        stmt = select(analysis_attempts).order_by(analysis_attempts.c.started_at)
        return (await connection.execute(stmt)).mappings().all()


async def expire_lease(database, task):
    expired = utcnow() - timedelta(seconds=1)
    async with database.transaction() as session:
        await session.execute(
            update(tasks).where(tasks.c.id == task.id).values(lease_expires_at=expired)
        )
        await session.execute(
            update(ai_calls).where(ai_calls.c.id == task.lease_id).values(deadline=expired)
        )


def assert_attribution(event, worker, lease_id):
    assert event["executor_id"] == worker.worker_id
    assert event["executor_session_id"] == str(worker.worker_session_id)
    assert event["attempt_id"] == str(lease_id)


async def test_worker_publishes_the_persisted_attempt_on_stage_and_terminal_job_events(
    database, workspace
):
    setup = Setup(database, workspace)
    await configure_ai(database)
    await accept(database, summary_message("美妆"))

    await setup.run_until(emitted(database, type="job", status="completed"), identity=OWNER)
    broker = StubBroker()
    await EventPublisher(database, broker, "analysis.events", "analysis_event").publish_pending()

    accepted, started, succeeded, completed = [
        Message.decode(encoded).args[0] for encoded in broker.queues["analysis.events"].queue
    ]
    [attempt] = await attempt_rows(database)
    async with database.engine.connect() as connection:
        [call] = (await connection.execute(select(ai_calls))).mappings().all()

    assert (accepted["executor_id"], accepted["executor_session_id"], accepted["attempt_id"]) == (
        None,
        None,
        None,
    )
    assert [event["status"] for event in (started, succeeded, completed)] == [
        "started",
        "succeeded",
        "completed",
    ]
    for event in (started, succeeded, completed):
        assert_attribution(event, OWNER, attempt["attempt_id"])
    assert accepted["retry_generation"] is None
    assert started["retry_generation"] == succeeded["retry_generation"] == 0
    assert completed["retry_generation"] == 0
    assert (attempt["job_external_id"], attempt["run"], attempt["stage"], attempt["segment"]) == (
        "s-1",
        1,
        "summarize",
        None,
    )
    assert attempt["attempt_no"] == 1
    assert attempt["status"] == AttemptStatus.SUCCEEDED
    assert attempt["started_at"] == datetime.fromisoformat(started["occurred_at"])
    assert attempt["ended_at"] == datetime.fromisoformat(succeeded["occurred_at"])
    assert attempt["error"] is None
    assert attempt["worker_session_id"] == OWNER.worker_session_id
    assert call["worker_id"] == attempt["worker_id"] == OWNER.worker_id
    assert (call["id"], call["task_id"]) == (attempt["attempt_id"], attempt["task_id"])


async def test_recovering_an_ai_lease_preserves_its_owner_and_closes_the_attempt(
    database, workspace
):
    setup = Setup(database, workspace)
    [pool] = await configured_pools(database)
    await accept(database, summary_message("美妆"))
    claimed = await setup.worker(identity=OWNER)._claim_ai(pool)
    assert claimed is not None
    task, profile = claimed
    [running] = await attempt_rows(database)
    assert running["attempt_id"] == task.lease_id
    assert running["status"] == AttemptStatus.RUNNING
    assert running["ended_at"] is None

    await expire_lease(database, task)
    await setup.run_until(emitted(database, type="job", status="needs_review"), identity=OTHER)

    [review_stage] = await events(database, type="stage", status="needs_review")
    [review_job] = await events(database, type="job", status="needs_review")
    for event in (review_stage, review_job):
        assert_attribution(event, OWNER, task.lease_id)

    async with database.transaction() as session:
        await TaskService(session).retry_job("s-1")
    await setup.run_until(emitted(database, type="job", status="completed"), identity=OTHER)
    await setup.pipeline.run(task, profile)  # 旧进程迟到的结果不能改写已结清的记录。

    expired, succeeded = await attempt_rows(database)
    assert expired["attempt_id"] == task.lease_id != succeeded["attempt_id"]
    assert expired["task_id"] == succeeded["task_id"] == task.id
    assert expired["attempt_no"] == succeeded["attempt_no"] == 1
    assert (expired["retry_generation"], succeeded["retry_generation"]) == (0, 1)
    assert expired["status"] == AttemptStatus.NEEDS_REVIEW
    assert expired["error"] == review_stage["error"] == "Worker 执行中断"
    assert expired["ended_at"] == datetime.fromisoformat(review_stage["occurred_at"])
    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                select(ai_calls.c.outcome).where(ai_calls.c.id == task.lease_id)
            )
            == CallOutcome.LOST
        )
    assert succeeded["status"] == AttemptStatus.SUCCEEDED
    [succeeded_stage] = await events(database, type="stage", status="succeeded")
    [completed] = await events(database, type="job", status="completed")
    assert succeeded_stage["retry_generation"] == completed["retry_generation"] == 1
    for event in (succeeded_stage, completed):
        assert_attribution(event, OTHER, succeeded["attempt_id"])


async def test_recovering_a_media_lease_retries_with_a_new_attempt_at_the_same_generation(
    database, workspace
):
    retry = RetryPolicy(max_attempts=2, base_delay=timedelta(0))
    setup = Setup(database, workspace, retry=retry)
    pools = await configured_pools(database)
    await accept(database, video_message())
    first = await setup.worker(identity=OWNER)._claim_fetch(pools)
    assert first is not None

    await expire_lease(database, first)
    await setup.pipeline.recover_expired()
    second = await setup.worker(identity=OTHER)._claim_fetch(pools)
    assert second is not None
    await setup.pipeline.run(first)
    await setup.pipeline.run(second)

    retired, current = await attempt_rows(database)
    assert (retired["attempt_id"], current["attempt_id"]) == (first.lease_id, second.lease_id)
    assert (retired["attempt_no"], current["attempt_no"]) == (1, 2)
    assert retired["retry_generation"] == current["retry_generation"] == 0
    assert retired["status"] == AttemptStatus.RETRYING
    assert current["status"] == AttemptStatus.SUCCEEDED
    [retrying] = await events(database, type="stage", status="retrying")
    assert_attribution(retrying, OWNER, retired["attempt_id"])
    assert retired["ended_at"] == datetime.fromisoformat(retrying["occurred_at"])
    assert retired["error"] == "Worker 执行中断"


async def test_a_later_review_does_not_reattribute_an_already_failed_job(database, workspace):
    setup = Setup(database, workspace)
    [pool] = await configured_pools(database)
    await accept(database, video_message())
    job = await load_job(database)
    async with database.transaction() as session:
        repository = TaskRepository(session)
        for segment in range(2):
            repository.add(Task(TaskKind.LISTEN, job.id, segment))
    first = await setup.worker(identity=OWNER)._claim_ai(pool)
    second = await setup.worker(identity=OTHER)._claim_ai(pool)
    assert first is not None and second is not None
    rejected, profile = first
    interrupted, _ = second

    await setup.pipeline._failed(rejected, Rejected("model rejected the audio"), profile)
    await expire_lease(database, interrupted)
    await setup.pipeline.recover_expired()

    failed, review = await attempt_rows(database)
    assert (failed["status"], failed["error"]) == (AttemptStatus.FAILED, "model rejected the audio")
    assert review["status"] == AttemptStatus.NEEDS_REVIEW
    assert {failed["segment"], review["segment"]} == {0, 1}
    [job_failure] = await events(database, type="job", status="failed")
    [stage_review] = await events(database, type="stage", status="needs_review")
    assert_attribution(job_failure, OWNER, rejected.lease_id)
    assert_attribution(stage_review, OTHER, interrupted.lease_id)


@pytest.mark.parametrize("status", [AttemptStatus.CANCELLED, AttemptStatus.SUPERSEDED])
async def test_deleted_tasks_leave_a_closed_attempt_in_history(database, workspace, status):
    setup = Setup(database, workspace)
    pools = await configured_pools(database)
    await accept(database, video_message())
    claimed = await setup.worker(identity=OWNER)._claim_fetch(pools)
    assert claimed is not None

    if status == AttemptStatus.CANCELLED:
        async with database.transaction() as session:
            await TaskService(session).cancel_job("j-1")
    else:
        await accept(database, video_message(run=2))
    await setup.pipeline.run(claimed)

    [attempt] = await attempt_rows(database)
    assert attempt["attempt_id"] == claimed.lease_id
    assert attempt["task_id"] == claimed.id
    assert attempt["status"] == status
    assert attempt["run"] == 1
    assert attempt["ended_at"] >= attempt["started_at"]
    async with database.engine.connect() as connection:
        assert await connection.scalar(select(tasks.c.id).where(tasks.c.id == claimed.id)) is None
    assert [event["status"] for event in await events(database, type="stage")] == ["started"]


async def test_attempt_creation_rolls_back_with_the_task_when_the_outbox_write_fails(
    database, workspace, monkeypatch
):
    setup = Setup(database, workspace)
    pools = await configured_pools(database)
    await accept(database, video_message())

    async def fail_outbox(*args):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(Outbox, "put", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await setup.worker(identity=OWNER)._claim_fetch(pools)

    assert await attempt_rows(database) == []
    assert (await load_job(database)).status == JobStatus.ACCEPTED
    async with database.engine.connect() as connection:
        [task] = (await connection.execute(select(tasks))).mappings().all()
    assert (task["status"], task["lease_id"], task["attempts"]) == (TaskStatus.READY, None, 0)
