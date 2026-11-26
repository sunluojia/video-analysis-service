import asyncio
import json
from datetime import timedelta

import pytest

from tests.factories import (
    accept,
    configure_ai,
    emitted,
    events,
    load_job,
    summary_message,
    video_message,
)
from tests.modules.scheduling.execution.fakes import FakeAi, FakeMedia, Setup, in_flight, wait_until
from video_analysis.core.common.clock import utcnow
from video_analysis.core.storage.workspace import AUDIO_SEGMENT_INDICES
from video_analysis.modules.ai.call.models import CallOutcome
from video_analysis.modules.ai.call.repository import CallRepository
from video_analysis.modules.ai.prompt.service import PromptService
from video_analysis.modules.analysis.batch.repository import BatchRepository
from video_analysis.modules.analysis.job.models import JobStatus
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.upstream.messages import SummarizeCreator
from video_analysis.modules.scheduling.execution.ports import InvalidAudio, Rejected
from video_analysis.modules.scheduling.task.models import RetryPolicy, Task, TaskKind, TaskStatus
from video_analysis.modules.scheduling.task.repository import TaskRepository
from video_analysis.modules.scheduling.task.service import TaskService

COMPLETED = {"type": "job", "status": "completed"}


@pytest.fixture
def setup(database, workspace) -> Setup:
    return Setup(database, workspace)


async def accept_videos(database, count: int) -> None:
    for index in range(count):
        await accept(database, video_message(f"j-{index}", content=f"video {index}".encode()))


def completed_jobs(database, count: int):
    async def reached() -> bool:
        return len(await events(database, **COMPLETED)) == count

    return reached


def downloads(setup: Setup, count: int):
    async def reached() -> bool:
        return setup.materials.downloads == count

    return reached


async def task_states(database) -> set[tuple[TaskKind, TaskStatus]]:
    job = await load_job(database)
    async with database.transaction() as session:
        return {(task.kind, task.status) for task in await TaskRepository(session).of_job(job.id)}


async def claim(database, kind: TaskKind, lease: timedelta) -> Task:
    """像 Worker 一样领取一项任务。"""
    async with database.transaction() as session:
        now = utcnow()
        task = await TaskRepository(session).next_task([kind], now, listen_limit=2)
        assert task is not None
        task.start(now, lease)
        return task


async def test_video_result_is_reported_with_every_stage(database, setup):
    await configure_ai(database)
    await accept(database, video_message())

    await setup.run_until(emitted(database, **COMPLETED))

    succeeded = await events(database, type="stage", status="succeeded")
    assert {(event["stage"], event["segment"]) for event in succeeded} == {
        ("fetch", None),
        ("extract_frames", None),
        ("extract_audio", None),
        ("listen", 0),
        ("listen", 1),
        ("recognize", None),
    }
    assert len(await events(database, type="stage", status="started")) == 6
    [frames] = await events(database, stage="extract_frames", status="succeeded")
    assert frames["result"]["frames"][1] == {
        "index": 1,
        "at": 10.0,
        "url": "/api/jobs/j-1/frames/1",
    }
    accepted, completed = await events(database, type="job")
    assert accepted["status"] == "accepted"
    assert completed["result"]["listening"]["transcript"] == "第000段\n第001段"
    assert completed["result"]["recognition"]["tags"] == ["美妆", "口播"]
    # 下载的视频在识别后删除，抽帧图片保留到保留期结束。
    job = await load_job(database)
    assert completed["artifacts_until"] is not None
    assert not setup.workspace.source(job.id).exists()
    assert len(setup.workspace.frames(job.id)) == 3


async def test_summary_tags_come_from_the_supplied_recognitions(database, setup):
    await configure_ai(database)
    await accept(database, summary_message("美妆", "穿搭"))

    await setup.run_until(emitted(database, job_id="s-1", **COMPLETED))

    [completed] = await events(database, job_id="s-1", **COMPLETED)
    assert completed["result"] == {
        "tags": ["美妆", "穿搭"],
        "videos": [{"job_id": "j-0", "run": 1}, {"job_id": "j-1", "run": 1}],
    }


async def test_video_without_audio_track_skips_listening(database, workspace):
    setup = Setup(database, workspace, media=FakeMedia(has_audio=False))
    await configure_ai(database)
    await accept(database, video_message())

    await setup.run_until(emitted(database, **COMPLETED))

    [skipped] = await events(database, status="skipped")
    assert (skipped["stage"], skipped["result"]) == (
        "extract_audio",
        {"skip_reason": "视频没有音轨，不需要听音"},
    )
    [fetched] = await events(database, stage="fetch", status="succeeded")
    assert skipped["executor_id"] == fetched["executor_id"] == "test-worker"
    assert skipped["executor_session_id"] == fetched["executor_session_id"]
    assert skipped["attempt_id"] is None
    assert len(setup.ai.prompts) == 1


async def test_no_usable_audio_is_reported_by_the_audio_stage(database, workspace):
    setup = Setup(database, workspace, media=FakeMedia(segments=0))
    await configure_ai(database)
    await accept(database, video_message())

    await setup.run_until(emitted(database, **COMPLETED))

    [audio] = await events(database, stage="extract_audio", status="succeeded")
    assert audio["result"]["skip_reason"] == "没有可听的音频片段"
    assert len(setup.ai.prompts) == 1


@pytest.mark.parametrize(
    ("message", "original_indices", "expected_transcript"),
    [
        ("The audio is empty", [0], ""),
        ("The audio format is illegal and cannot be opened", [0, 2], "第001段"),
    ],
)
async def test_invalid_audio_is_audited_and_video_and_creator_still_complete(
    database, workspace, monkeypatch, caplog, message, original_indices, expected_transcript
):
    media = FakeMedia(segments=len(original_indices))
    media.frame_count = 6
    setup = Setup(database, workspace, media=media, retry=RetryPolicy(max_attempts=3))
    extract_audio = media.extract_audio
    listen = setup.ai.listen
    recognize = setup.ai.recognize

    async def extract_with_indices(source, target):
        count = await extract_audio(source, target)
        (target / AUDIO_SEGMENT_INDICES).write_text(json.dumps(original_indices), encoding="utf-8")
        return count

    async def reject_first_segment(profile, audio):
        assert audio.is_file()
        if audio.stem == "000":
            raise InvalidAudio(f"Error code: 400 - {message}")
        return await listen(profile, audio)

    async def recognize_six_frames(profile, material):
        assert [path.name for path in material.frames] == [f"{i:03d}.jpg" for i in range(6)]
        assert material.listening.transcript == expected_transcript
        return await recognize(profile, material)

    monkeypatch.setattr(media, "extract_audio", extract_with_indices)
    monkeypatch.setattr(setup.ai, "listen", reject_first_segment)
    monkeypatch.setattr(setup.ai, "recognize", recognize_six_frames)
    await configure_ai(database)
    await accept(database, video_message())
    await setup.run_until(emitted(database, job_id="j-1", **COMPLETED))

    [completed] = await events(database, job_id="j-1", **COMPLETED)
    assert completed["result"]["listening"] == {
        "sounds": ["speech"] if expected_transcript else [],
        "transcript": expected_transcript,
    }
    [stage] = await events(database, stage="listen", segment=0, status="succeeded")
    assert stage["result"] == {"sounds": [], "transcript": ""}
    [audio] = await events(database, stage="extract_audio", status="succeeded")
    segments = audio["result"]["segments"]
    assert [(item["index"], item["start"], item["end"]) for item in segments] == [
        (index, original * 10, (original + 1) * 10)
        for index, original in enumerate(original_indices)
    ]
    job = await load_job(database)
    async with database.transaction() as session:
        calls = await CallRepository(session).of_job(job.id)
    listening = [call for call in calls if call["kind"] == TaskKind.LISTEN]
    assert len(listening) == len(original_indices)
    [rejected] = [call for call in listening if call["outcome"] == CallOutcome.FAILED]
    assert rejected["error"] == f"Error code: 400 - {message}"
    assert rejected["finished_at"] is not None
    assert rejected["input_tokens"] is None
    assert "听音降级" in caplog.text and message in caplog.text

    recognition = completed["result"]["recognition"]
    summary_input = SummarizeCreator.model_validate(
        {
            "job_id": "s-1",
            "run": 1,
            "batch_id": "b-1",
            "creator_id": "c-1",
            "videos": [{"job_id": "j-1", "run": 1, "recognition": recognition}],
        }
    )
    await accept(database, summary_input)
    await setup.run_until(emitted(database, job_id="s-1", **COMPLETED))
    [summary] = await events(database, job_id="s-1", **COMPLETED)
    assert "美妆" in summary["result"]["tags"]


@pytest.mark.parametrize(
    ("error", "status", "outcome", "attempts"),
    [
        (Rejected("DataInspectionFailed: inappropriate content"), "failed", CallOutcome.FAILED, 1),
        (TimeoutError("no response"), "needs_review", CallOutcome.TIMED_OUT, 1),
        (OSError("connection reset"), "failed", CallOutcome.FAILED, 3),
    ],
    ids=["content_inspection", "timeout", "network_error"],
)
async def test_other_listen_errors_keep_their_original_handling(
    database, workspace, monkeypatch, error, status, outcome, attempts
):
    setup = Setup(
        database,
        workspace,
        media=FakeMedia(segments=1),
        retry=RetryPolicy(max_attempts=3, base_delay=timedelta(0)),
    )

    async def fail_listen(profile, audio):
        raise error

    monkeypatch.setattr(setup.ai, "listen", fail_listen)
    await configure_ai(database)
    await accept(database, video_message())
    await setup.run_until(emitted(database, type="job", status=status))

    job = await load_job(database)
    async with database.transaction() as session:
        calls = await CallRepository(session).of_job(job.id)
    assert job.status == status
    assert len(calls) == attempts
    for call in calls:
        assert call["kind"] == TaskKind.LISTEN
        assert call["outcome"] == outcome
        assert call["error"] == str(error)
    assert (TaskKind.LISTEN, TaskStatus(status)) in await task_states(database)
    assert await events(database, stage="recognize") == []


async def test_ai_requests_never_exceed_model_capacity(database, setup):
    await configure_ai(database, capacity=2)
    await accept_videos(database, 3)
    setup.ai.gate.clear()

    async with setup.running():
        await wait_until(in_flight(setup.ai, 2))
        await asyncio.sleep(0.3)  # 额度占满期间 Worker 又轮询了几轮，不应再发出请求
        assert setup.ai.in_flight == 2
        setup.ai.gate.set()
        await wait_until(completed_jobs(database, 3))

    assert setup.ai.peak == 2


async def test_requests_in_flight_keep_their_model_when_routes_change(database, workspace):
    setup = Setup(database, workspace, media=FakeMedia(segments=0))
    await configure_ai(database, model="old-model", capacity=1)
    await accept_videos(database, 2)
    setup.ai.gate.clear()

    async with setup.running():
        await wait_until(in_flight(setup.ai, 1))
        await configure_ai(database, model="new-model", capacity=1)
        # 旧模型的在途请求仍计入旧模型，新模型用自己的额度领取下一项。
        await wait_until(in_flight(setup.ai, 2))
        setup.ai.gate.set()
        await wait_until(completed_jobs(database, 2))

    assert setup.ai.models == ["old-model", "new-model"]


async def test_new_videos_start_only_while_models_need_more_work(database, setup):
    await configure_ai(database, capacity=1)
    await accept_videos(database, 4)
    setup.materials.gate.clear()

    async with setup.running(admission_factor=2):
        await wait_until(downloads(setup, 2))
        await asyncio.sleep(0.3)  # 下载中的视频算作即将到来的模型工作，额度的 2 倍已经够用
        assert setup.materials.downloads == 2


async def test_workers_admit_new_videos_one_at_a_time(database, setup, monkeypatch):
    await configure_ai(database, capacity=1)
    await accept_videos(database, 4)
    setup.materials.gate.clear()
    count = TaskRepository.preparing_videos
    checks = 0

    async def slow_count(repository):
        nonlocal checks
        result = await count(repository)
        await asyncio.sleep(0.1)  # 拉长计数到领取之间的窗口，让两个 Worker 的判断重叠
        checks += 1
        return result

    async def checked_twice_each() -> bool:
        return checks >= 4

    # 额度 1 时只够备一个视频；两个 Worker 若同时看到 0，会各自开始一个。
    monkeypatch.setattr(TaskRepository, "preparing_videos", slow_count)
    async with setup.running(workers=2, admission_factor=1):
        await wait_until(checked_twice_each)
        async with database.transaction() as session:
            assert await JobRepository(session).processing_videos() == 1


async def test_stale_execution_cannot_overwrite_a_retried_task(database, setup):
    await configure_ai(database)
    await accept(database, video_message())

    stale = await claim(database, TaskKind.FETCH, timedelta(0))
    await setup.pipeline.recover_expired()
    async with database.transaction() as session:
        await TaskService(session).retry_job("j-1")
    current = await claim(database, TaskKind.FETCH, timedelta(minutes=1))

    await setup.pipeline.run(stale)
    assert await task_states(database) == {(TaskKind.FETCH, TaskStatus.RUNNING)}

    await setup.pipeline.run(current)
    assert await task_states(database) == {
        (TaskKind.FETCH, TaskStatus.SUCCEEDED),
        (TaskKind.EXTRACT_FRAMES, TaskStatus.READY),
        (TaskKind.EXTRACT_AUDIO, TaskStatus.READY),
    }


async def test_timed_out_request_waits_for_review_instead_of_resending(database, workspace):
    setup = Setup(database, workspace, ai=FakeAi(slow={"美妆"}), media=FakeMedia(segments=0))
    await configure_ai(database, timeout=0.2)
    await accept(database, video_message())

    await setup.run_until(emitted(database, type="job", status="needs_review"))
    [review] = await events(database, type="stage", status="needs_review")
    assert review["stage"] == "recognize"
    assert await events(database, **COMPLETED) == []

    async with database.transaction() as session:
        job = await TaskService(session).retry_job("j-1")
    assert job.status == JobStatus.PROCESSING
    await setup.run_until(emitted(database, **COMPLETED))


async def test_retry_after_files_expire_starts_again_from_the_video(database, workspace):
    """抽帧图片和音频段过了保留期再补跑：重新下载视频、抽帧，不拿空图片去识别。"""
    setup = Setup(
        database,
        workspace,
        ai=FakeAi(slow={"美妆"}),
        media=FakeMedia(segments=0),
        retention=timedelta(0),
    )
    await configure_ai(database, timeout=0.2)
    await accept(database, video_message())
    await setup.run_until(emitted(database, type="job", status="needs_review"))
    await setup.pipeline.discard_expired()

    async with database.transaction() as session:
        job = await TaskService(session).retry_job("j-1")
    assert job.status == JobStatus.PROCESSING
    assert await task_states(database) == {(TaskKind.FETCH, TaskStatus.READY)}

    await setup.run_until(emitted(database, **COMPLETED))
    assert setup.materials.downloads == 2
    for stage in ("fetch", "recognize"):
        started = await events(database, stage=stage, status="started")
        assert [event["retry_generation"] for event in started] == [0, 1]
        assert [event["attempt"] for event in started] == [1, 1]


async def test_recognition_without_frames_fails_instead_of_guessing(database, workspace):
    media = FakeMedia(segments=0)
    media.frame_count = 0
    setup = Setup(database, workspace, media=media)
    await configure_ai(database)
    await accept(database, video_message())

    await setup.run_until(emitted(database, type="job", status="failed"))
    assert (TaskKind.RECOGNIZE, TaskStatus.FAILED) in await task_states(database)
    assert setup.ai.models == []


async def test_rejected_request_fails_at_once_instead_of_retrying(database, workspace):
    setup = Setup(
        database,
        workspace,
        ai=FakeAi(rejected={"美妆"}),
        media=FakeMedia(segments=0),
        retry=RetryPolicy(max_attempts=3),
    )
    await configure_ai(database)
    await accept(database, video_message())

    await setup.run_until(emitted(database, type="job", status="failed"))
    assert (TaskKind.RECOGNIZE, TaskStatus.FAILED) in await task_states(database)
    assert setup.ai.models == ["test-model"]
    [stage] = await events(database, type="stage", status="failed")
    [job] = await events(database, type="job", status="failed")
    assert job["executor_id"] == stage["executor_id"] == "test-worker"
    assert job["executor_session_id"] == stage["executor_session_id"]
    assert job["attempt_id"] == stage["attempt_id"]


async def test_new_run_analyzes_again_with_the_newly_activated_prompt(database, setup):
    await configure_ai(database)
    await accept(database, video_message())
    await setup.run_until(emitted(database, run=1, **COMPLETED))

    async with database.transaction() as session:
        prompt = await PromptService(session).add_version(
            TaskKind.RECOGNIZE, "只输出品类标签", "", make_active=True
        )
    await accept(database, video_message(run=2, allow_reuse=False))
    await setup.run_until(emitted(database, run=2, **COMPLETED))

    assert setup.ai.prompts.count("只输出品类标签") == 1
    assert setup.materials.downloads == 2
    job = await load_job(database)
    async with database.transaction() as session:
        calls = await CallRepository(session).of_job(job.id)
    recognitions = [call for call in calls if call["kind"] == TaskKind.RECOGNIZE]
    assert [call["prompt_version"] for call in recognitions] == [None, prompt.version]


async def test_identical_analysis_reuses_the_earlier_result(database, setup):
    await configure_ai(database)
    await accept(database, video_message("j-1"))
    await setup.run_until(emitted(database, job_id="j-1", **COMPLETED))
    calls = len(setup.ai.prompts)

    await accept(database, video_message("j-2"))
    await setup.run_until(emitted(database, job_id="j-2", **COMPLETED))

    [original] = await events(database, job_id="j-1", **COMPLETED)
    [reused] = await events(database, job_id="j-2", **COMPLETED)
    assert reused["reused_from"] == "j-1"
    assert reused["result"] == original["result"]
    assert (setup.materials.downloads, len(setup.ai.prompts)) == (1, calls)

    # 换了提示词，结果不再相同，重新分析。
    async with database.transaction() as session:
        await PromptService(session).add_version(
            TaskKind.RECOGNIZE, "只输出品类标签", "", make_active=True
        )
    await accept(database, video_message("j-3"))
    await setup.run_until(emitted(database, job_id="j-3", **COMPLETED))

    [fresh] = await events(database, job_id="j-3", **COMPLETED)
    assert fresh["reused_from"] is None


async def test_cancelled_job_stops_and_its_files_are_removed(database, setup):
    await configure_ai(database)
    await accept(database, video_message())
    job = await load_job(database)
    setup.ai.gate.clear()

    async def files_removed() -> bool:
        return not setup.workspace.frames(job.id)

    async with setup.running():
        await wait_until(in_flight(setup.ai, 2))
        async with database.transaction() as session:
            cancelled = await TaskService(session).cancel_job("j-1")
        assert cancelled.status == JobStatus.CANCELLED
        setup.ai.gate.set()
        await wait_until(files_removed)

    assert await task_states(database) == set()
    assert await events(database, **COMPLETED) == []


async def test_paused_batch_waits_until_resumed(database, setup):
    await configure_ai(database)
    async with database.transaction() as session:
        await BatchRepository(session).set_paused("b-1", True)
    await accept(database, video_message())

    async with setup.running():
        await asyncio.sleep(0.3)
        assert setup.materials.downloads == 0
        async with database.transaction() as session:
            await BatchRepository(session).set_paused("b-1", False)
        await wait_until(emitted(database, **COMPLETED))


async def test_files_are_removed_once_the_retention_period_ends(database, workspace):
    setup = Setup(database, workspace, retention=timedelta(0))
    await configure_ai(database)
    await accept(database, video_message())
    job = await load_job(database)

    async def removed() -> bool:
        return not setup.workspace.frames(job.id)

    async with setup.running():
        await wait_until(emitted(database, **COMPLETED))
        await wait_until(removed)

    assert (await load_job(database)).artifacts_until is None


async def test_retention_cleanup_can_be_disabled(database, workspace):
    setup = Setup(database, workspace, retention=timedelta(0))
    await configure_ai(database)
    await accept(database, video_message())
    job = await load_job(database)

    async with setup.running(artifact_cleanup_enabled=False):
        await wait_until(emitted(database, **COMPLETED))
        await accept(database, summary_message("美妆"))
        await wait_until(emitted(database, **COMPLETED, kind="summary"))

    assert setup.workspace.frames(job.id)
    assert (await load_job(database)).artifacts_until <= utcnow()
