import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from tests.factories import accept, events, load_job, summary_message, video_message
from tests.modules.ai.batch.test_payload import URL
from tests.modules.scheduling.execution.fakes import FakeMedia, Setup
from video_analysis.core.common.clock import utcnow
from video_analysis.integrations.llm.batch_payload import build_line
from video_analysis.modules.ai.batch.manager import LEASE, BatchLimits, BatchManager
from video_analysis.modules.ai.batch.models import BatchItem, BatchJob
from video_analysis.modules.ai.batch.repository import BatchJobRepository
from video_analysis.modules.ai.call.service import CallService
from video_analysis.modules.ai.call.tables import ai_calls
from video_analysis.modules.ai.config.models import AiModel, AiProvider
from video_analysis.modules.ai.config.repository import AiConfigRepository
from video_analysis.modules.ai.config.service import AiConfigService
from video_analysis.modules.ai.quota.repository import QuotaRepository
from video_analysis.modules.analysis.batch.repository import BatchRepository
from video_analysis.modules.analysis.job.models import JobStatus
from video_analysis.modules.scheduling.task.models import (
    AI_KINDS,
    RetryPolicy,
    TaskKind,
    TaskStatus,
    WorkerIdentity,
)
from video_analysis.modules.scheduling.task.repository import TaskRepository
from video_analysis.modules.scheduling.task.tables import tasks


class FakeBailian:
    def __init__(self):
        self.payloads = []
        self.files = {}
        self.runs = {}
        self.upload_error = False
        self.ambiguous_submit = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        pass

    async def upload(self, filename, payload):
        if self.upload_error:
            raise OSError("do not retain this provider detail")
        rows = [json.loads(line) for line in payload.splitlines()]
        self.payloads.append(rows)
        return f"file-{len(self.payloads)}"

    async def submit(self, file_id):
        run = SimpleNamespace(
            id=f"remote-{file_id}",
            input_file_id=file_id,
            status="in_progress",
            errors=None,
            request_counts=None,
            output_file_id=None,
            error_file_id=None,
        )
        self.runs[run.id] = run
        if self.ambiguous_submit:
            raise OSError("reply lost")
        return run

    async def retrieve(self, batch_id):
        return self.runs[batch_id]

    async def find(self, file_id, _since):
        return self.runs.get(f"remote-{file_id}")

    async def results(self, file_id):
        return self.files[file_id]

    def finish(self, *, status="completed", outputs=None, errors=None, code=None, line=None):
        [run] = self.runs.values()
        run.status = status
        run.errors = SimpleNamespace(data=[SimpleNamespace(code=code, line=line)]) if code else None
        run.request_counts = SimpleNamespace(completed=len(outputs or {}), failed=len(errors or {}))
        if outputs:
            self.files["outputs"] = outputs
            run.output_file_id = "outputs"
        if errors:
            self.files["errors"] = errors
            run.error_file_id = "errors"


def manager(setup, provider, *, items=20, max_bytes=80_000_000, seconds=120):
    return BatchManager(
        setup.database,
        setup.pipeline,
        BatchLimits(items, max_bytes, timedelta(seconds=seconds), timedelta(), 3),
        WorkerIdentity("batch-worker", uuid4()),
        lambda _profile: provider,
    )


async def configure_batch_route(database):
    async with database.transaction() as session:
        providers = AiConfigRepository(session)
        provider = AiProvider("Bailian", "openai", URL)
        model = AiModel(provider.id, "qwen3.8-flash", 1, 20, 5)
        providers.add(provider)
        providers.add(model)
        for kind in AI_KINDS:
            await AiConfigService(session).save_route(kind, model.id, share=1)


async def register_summaries(database, count, *, age=0):
    for i in range(count):
        message = summary_message("makeup", job_id=f"summary-{i}")
        await accept(database, message.model_copy(update={"ai_batch_mode": True}))
    if age:
        async with database.transaction() as session:
            await session.execute(update(tasks).values(run_after=utcnow() - timedelta(seconds=age)))


def output(custom_id, body, tool=None):
    msg = (
        {"content": json.dumps(body)}
        if tool is None
        else {"tool_calls": [{"function": {"name": tool, "arguments": json.dumps(body)}}]}
    )
    return {
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "body": {
                "choices": [{"message": msg}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                },
            },
        },
    }


async def get_tasks(database):
    async with database.transaction() as session:
        return (await session.execute(select(tasks).order_by(tasks.c.created_at))).mappings().all()


@pytest.mark.parametrize("trigger", ["items", "bytes", "age"])
async def test_packer_flushes_and_protects_leases(database, workspace, trigger):
    await configure_batch_route(database)
    await register_summaries(database, 2, age=150 if trigger == "age" else 0)
    setup = Setup(database, workspace)
    bailian = FakeBailian()
    max_bytes = 80_000_000
    if trigger == "bytes":
        [first, *_] = await get_tasks(database)
        async with database.transaction() as session:
            task = await TaskRepository(session).next_task([TaskKind.SUMMARIZE], utcnow(), 2)
            profile = await CallService(session).routed(TaskKind.SUMMARIZE)
        material = await setup.pipeline.prepare_ai(task)
        max_bytes = len(
            build_line(str(first.id), profile, TaskKind.SUMMARIZE, material, max_bytes).data
        )
    runner = manager(setup, bailian, items=2 if trigger == "items" else 20, max_bytes=max_bytes)
    await runner.tick()

    [packed] = bailian.payloads
    expected = 1 if trigger == "bytes" else 2
    assert len(packed) == expected
    assert len({row["custom_id"] for row in packed}) == expected
    rows = await get_tasks(database)
    running = [row for row in rows if row.status == TaskStatus.RUNNING]
    assert len(running) == expected
    assert all(row.lease_expires_at - row.started_at == LEASE for row in running)
    async with database.transaction() as session:
        [job] = await BatchJobRepository(session).active()
        [pool] = await QuotaRepository(session).pools()
        assert job.request_count == expected and job.remote_batch_id
        assert await QuotaRepository(session).running(pool) == {}
        assert await QuotaRepository(session).recent_requests(pool, utcnow()) == 0
        assert await TaskRepository(session).expired(utcnow() + LEASE + timedelta(hours=1)) == []


async def test_waiting_batch_work_does_not_block_fetch_or_take_realtime_slots(database, workspace):
    await configure_batch_route(database)
    await register_summaries(database, 1)
    await accept(database, video_message())
    setup = Setup(database, workspace)
    async with database.transaction() as session:
        pools = await QuotaRepository(session).pools()
        assert TaskKind.LISTEN not in pools[0].batch_kinds
    worker = setup.worker(admission_factor=1, backlog=1)
    assert await worker._claim_ai(pools[0]) is None
    assert (await worker._claim_fetch(pools)).kind == TaskKind.FETCH
    assert await worker._claim_fetch(pools) is None
    bailian = FakeBailian()
    await manager(setup, bailian).tick()
    assert not bailian.payloads  # An unfilled group still waits while fetch can proceed.
    async with database.transaction() as session:
        await BatchRepository(session).set_ai_batch_mode("b-1", False)
    assert (await worker._claim_ai(pools[0]))[0].kind == TaskKind.SUMMARIZE


async def test_poller_recognition_uses_pipeline_filters_usage_and_discard(
    database, workspace, client
):
    await configure_batch_route(database)
    await accept(database, video_message().model_copy(update={"ai_batch_mode": True}))
    setup = Setup(database, workspace, media=FakeMedia(has_audio=False))

    async def prepared():
        return any(row.kind == TaskKind.RECOGNIZE for row in await get_tasks(database))

    await setup.run_until(prepared)
    bailian = FakeBailian()
    runner = manager(setup, bailian, items=1)
    await runner.tick()
    [[item]] = bailian.payloads
    status = (await client.get("/api/status")).json()
    [active] = status["active_batch_jobs"]
    assert active["remote_batch_id"] and active["batch_ids"] == ["b-1"]
    assert status["ai_batch_mode"] == {"b-1": True}
    job = await load_job(database)
    assert workspace.source(job.id).is_file()

    custom_id = item["custom_id"]
    result = {
        "tags": ["makeup"],
        "summary": "summary",
        "appearances": [
            {"frame_index": 1, "age_group": "25-34", "evidence": "face"},
            {"frame_index": 50, "age_group": "35-44", "evidence": "out of range"},
            {"frame_index": 0, "age_group": "35-44", "evidence": " "},
        ],
    }
    bailian.finish(outputs={custom_id: output(custom_id, result, "Recognition")})
    await runner.tick()
    job = await load_job(database)
    assert job.status == JobStatus.COMPLETED
    assert [a["frame_index"] for a in job.result["recognition"]["appearances"]] == [1]
    assert not workspace.source(job.id).exists()
    async with database.transaction() as session:
        [call] = (await session.execute(select(ai_calls))).mappings().all()
        assert call.ai_batch and (call.input_tokens, call.output_tokens) == (12, 5)
        [item] = await BatchJobRepository(session).list_all(BatchItem)
        assert item.status == "succeeded"
    assert await events(database, type="stage", stage="recognize", status="succeeded")


async def test_poller_settles_partial_errors_and_missing_lines(database, workspace):
    await configure_batch_route(database)
    await register_summaries(database, 3)
    setup = Setup(database, workspace, retry=RetryPolicy(3, timedelta(hours=1)))
    bailian = FakeBailian()
    runner = manager(setup, bailian, items=3)
    await runner.tick()
    [rows] = bailian.payloads
    succeeded, rejected, _ = [item["custom_id"] for item in rows]
    bailian.finish(
        outputs={succeeded: output(succeeded, {"tags": ["makeup", "99\u5c81"]})},
        errors={rejected: {"error": {"code": "DataInspectionFailed"}}},
    )
    await runner.tick()
    assert {row.status for row in await get_tasks(database)} == {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.READY,
    }
    async with database.transaction() as session:
        [job] = await BatchJobRepository(session).list_all(BatchJob)
        items = await BatchJobRepository(session).list_all(BatchItem)
        assert job.status == "completed" and job.finished_at is not None
        assert (job.completed_count, job.failed_count) == (1, 1)
        assert {item.status for item in items} == {"succeeded", "failed"}
    [done] = await events(database, type="job", status="completed")
    assert done["result"]["tags"] == ["makeup"]


@pytest.mark.parametrize("status", ["failed", "expired", "cancelled"])
async def test_poller_settles_terminal_jobs(database, workspace, status):
    await configure_batch_route(database)
    await register_summaries(database, 1)
    setup = Setup(database, workspace, retry=RetryPolicy(3, timedelta(hours=1)))
    bailian = FakeBailian()
    runner = manager(setup, bailian, items=1)
    await runner.tick()
    bailian.finish(status=status, code="invalid_prompt" if status == "failed" else None)
    await runner.tick()
    [task] = await get_tasks(database)
    assert task.status == (TaskStatus.FAILED if status == "failed" else TaskStatus.READY)
    assert task.lease_expires_at is None


async def test_submit_retries_upload_failure_without_leaving_lease(database, workspace):
    await configure_batch_route(database)
    await register_summaries(database, 1)
    setup = Setup(database, workspace, retry=RetryPolicy(3, timedelta(hours=1)))
    bailian = FakeBailian()
    bailian.upload_error = True
    await manager(setup, bailian, items=1).tick()
    [task] = await get_tasks(database)
    assert task.status == TaskStatus.READY and task.lease_expires_at is None
    assert task.run_after > utcnow()
    assert "provider detail" not in task.error


async def test_ambiguous_create_reconciles_with_uploaded_file(database, workspace):
    await configure_batch_route(database)
    await register_summaries(database, 1)
    bailian = FakeBailian()
    bailian.ambiguous_submit = True
    runner = manager(Setup(database, workspace), bailian, items=1)
    await runner.tick()
    [[item]] = bailian.payloads
    custom_id = item["custom_id"]
    bailian.finish(outputs={custom_id: output(custom_id, {"tags": ["makeup"]})})
    await runner.tick()
    [task] = await get_tasks(database)
    assert task.status == TaskStatus.SUCCEEDED and task.attempts == 1
    assert len(bailian.payloads) == 1


async def test_analysis_toggle_wins_over_queued_messages(client, database):
    await accept(database, video_message("old").model_copy(update={"ai_batch_mode": True}))
    path = "/api/batches/b-1/ai-batch-mode"
    assert (await client.put(path, json={"enabled": False})).status_code == 204
    await accept(database, video_message("queued").model_copy(update={"ai_batch_mode": True}))
    await accept(database, video_message("legacy", batch="legacy-batch"))
    async with database.transaction() as session:
        assert await BatchRepository(session).ai_batch_modes() == {
            "b-1": False,
            "legacy-batch": False,
        }
    assert (await client.put(path, json={"enabled": True})).status_code == 204
    assert (await client.get("/api/status")).json()["ai_batch_mode"]["b-1"] is True


async def test_rejected_file_only_rejects_items_with_validation_errors(database, workspace):
    await configure_batch_route(database)
    await register_summaries(database, 2)
    setup = Setup(database, workspace, retry=RetryPolicy(3, timedelta(hours=1)))
    bailian = FakeBailian()
    runner = manager(setup, bailian, items=2)
    await runner.tick()
    bailian.finish(status="failed", code="invalid_prompt", line=2)
    await runner.tick()
    assert [row.status for row in await get_tasks(database)] == [
        TaskStatus.READY,
        TaskStatus.FAILED,
    ]


async def test_oversized_recognition_is_available_to_the_realtime_worker(database, workspace):
    await configure_batch_route(database)
    await accept(database, video_message().model_copy(update={"ai_batch_mode": True}))
    setup = Setup(database, workspace, media=FakeMedia(has_audio=False))

    async def prepared():
        return any(row.kind == TaskKind.RECOGNIZE for row in await get_tasks(database))

    await setup.run_until(prepared)
    job = await load_job(database)
    workspace.frames(job.id)[0].write_bytes(b"x" * 800_000)
    bailian = FakeBailian()
    await manager(setup, bailian, items=1).tick()
    assert not bailian.payloads
    async with database.transaction() as session:
        [pool] = await QuotaRepository(session).pools()
    task, _ = await setup.worker()._claim_ai(pool)
    assert task.kind == TaskKind.RECOGNIZE and task.ai_batch_fallback and task.attempts == 1
