"""执行单项任务，并在同一事务里保存结果、推进后继阶段、写入回传事件。"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from video_analysis.core.common.clock import utcnow
from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace
from video_analysis.modules.ai.call.models import AiProfile, CallOutcome, Usage
from video_analysis.modules.ai.call.repository import CallRepository
from video_analysis.modules.ai.call.service import CallService
from video_analysis.modules.analysis.job.models import Job, JobKind, JobStatus, MediaInfo
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.job.results import Listening, merge_listening
from video_analysis.modules.analysis.upstream.service import UpstreamService
from video_analysis.modules.scheduling.execution.ports import (
    InvalidAudio,
    Materials,
    Media,
    Rejected,
    VideoAi,
    VideoMaterial,
)
from video_analysis.modules.scheduling.task.attempts import AttemptRepository
from video_analysis.modules.scheduling.task.models import (
    RetryPolicy,
    Task,
    TaskKind,
    TaskStatus,
    WorkerIdentity,
)
from video_analysis.modules.scheduling.task.repository import TaskRepository

log = logging.getLogger(__name__)

PREPARATION = frozenset({TaskKind.EXTRACT_FRAMES, TaskKind.EXTRACT_AUDIO, TaskKind.LISTEN})
"""完成后需要检查能否开始识别的阶段。"""


class WorkerLost(Exception):
    """任务租约到期仍未结束，执行它的 Worker 已中断。"""


class Pipeline:
    def __init__(
        self,
        database: Database,
        workspace: Workspace,
        materials: Materials,
        media: Media,
        ai: VideoAi,
        retry: RetryPolicy,
        media_timeout: timedelta,
        retention: timedelta,
    ) -> None:
        self._database = database
        self._workspace = workspace
        self._materials = materials
        self._media = media
        self._ai = ai
        self._retry = retry
        self._media_timeout = media_timeout
        self._retention = retention

    async def run(self, claimed: Task, profile: AiProfile | None = None) -> None:
        """执行一项已领取的任务。AI 任务使用领取时确定的配置，执行期间不再读取路由。"""
        try:
            if profile is None:
                output, usage = await self._process_media(claimed), None
            else:
                output, usage = await self._ask_ai(claimed, profile)
        except Exception as error:
            message = describe(error)
            if claimed.kind == TaskKind.LISTEN and isinstance(error, InvalidAudio):
                log.warning(
                    "视频 %s 音频段 %s 听音降级：%s", claimed.job_id, claimed.segment, message
                )
                await self._succeeded(
                    claimed, Listening(sounds=[], transcript=""), profile, None, call_error=message
                )
            else:
                log.warning("任务 %s %s 失败：%s", claimed.kind, claimed.id, message)
                await self._failed(claimed, error, profile)
        else:
            await self._succeeded(claimed, output, profile, usage)

    async def recover_expired(self) -> None:
        """回收租约到期的任务：媒体作业重新排队，AI 请求转人工核查。"""
        async with self._database.transaction() as session:
            now = utcnow()
            await CallRepository(session).expire(now)
            for task in await TaskRepository(session).expired(now):
                await self._record_failure(session, task, WorkerLost("Worker 执行中断"), now)

    async def discard_expired(self) -> None:
        """清理过了保留期的抽帧图片和音频段。"""
        async with self._database.transaction() as session:
            for job in await JobRepository(session).artifacts_expired(utcnow()):
                await self._workspace.discard(job.id)
                job.artifacts_until = None

    async def _process_media(self, task: Task) -> MediaInfo | Job | int | list[int]:
        job = await self._job(task.job_id)
        source = self._workspace.source(job.id)
        async with asyncio.timeout(self._media_timeout.total_seconds()):
            if task.kind == TaskKind.FETCH:
                return await self._fetch(job, source)
            if task.kind == TaskKind.EXTRACT_FRAMES:
                target = await self._workspace.fresh(job.id, "frames")
                return await self._media.extract_frames(source, target)
            target = await self._workspace.fresh(job.id, "audio")
            count = await self._media.extract_audio(source, target)
            return self._workspace.segment_indices(target, count)

    async def _fetch(self, job: Job, source: Path) -> MediaInfo | Job:
        """有相同分析的结果时直接采用；否则下载、校验并读取媒体信息。"""
        if job.video.allow_reuse:
            async with self._database.transaction() as session:
                key = await self._analysis_key(session, job)
                earlier = await JobRepository(session).reusable(key)
            if earlier:
                return earlier
        await self._materials.download(job.video.material, source)
        return await self._media.probe(source)

    async def _ask_ai(self, task: Task, profile: AiProfile) -> tuple[Any, Usage | None]:
        """返回阶段的输出和这次调用的 token 用量。"""
        job = await self._job(task.job_id)
        async with asyncio.timeout(profile.timeout_seconds):
            if task.kind == TaskKind.SUMMARIZE:
                videos = [video.recognition for video in job.summary.videos]
                answer = await self._ai.summarize(profile, videos)
                return answer.value, answer.usage

            if task.kind == TaskKind.LISTEN:
                assert task.segment is not None, "听音任务对应一个音频段"
                segment = self._workspace.segment(job.id, task.segment)
                answer = await self._ai.listen(profile, segment)
                return answer.value, answer.usage

            frames = self._workspace.frames(job.id)
            if not frames:
                raise Rejected("没有抽帧图片，不能识别；请重新分析这个视频")
            video, listening = job.video, await self._listening(job.id)
            material = VideoMaterial(frames, listening, video.description, video.topics)
            answer = await self._ai.recognize(profile, material)
            return (listening, answer.value), answer.usage

    async def _job(self, job_id: UUID) -> Job:
        async with self._database.transaction() as session:
            return await JobRepository(session).require(Job, job_id)

    async def _listening(self, job_id: UUID) -> Listening:
        async with self._database.transaction() as session:
            return merge_listening(await TaskRepository(session).listen_results(job_id))

    async def _succeeded(
        self,
        claimed: Task,
        output,
        profile: AiProfile | None,
        usage: Usage | None,
        *,
        call_error: str | None = None,
    ) -> None:
        """保存已完成的阶段，听音降级时调用本身记为失败。"""
        async with self._database.transaction() as session:
            now = utcnow()
            if claimed.kind.uses_ai:
                outcome = CallOutcome.SUCCEEDED if call_error is None else CallOutcome.FAILED
                await CallRepository(session).finish(
                    claimed, outcome, now, error=call_error, usage=usage
                )
            task = await self._current(session, claimed)
            if task is None:
                return
            await self._advance(session, task, output, profile, now)
        if claimed.kind == TaskKind.RECOGNIZE:
            # 识别已保存，下载的视频不再需要；抽帧图片和音频段保留到保留期结束。
            await self._workspace.discard_source(claimed.job_id)

    async def _failed(self, claimed: Task, error: Exception, profile: AiProfile | None) -> None:
        async with self._database.transaction() as session:
            now = utcnow()
            if claimed.kind.uses_ai:
                outcome = (
                    CallOutcome.TIMED_OUT if isinstance(error, TimeoutError) else CallOutcome.FAILED
                )
                await CallRepository(session).finish(claimed, outcome, now, describe(error))
            task = await self._current(session, claimed)
            if task is not None:
                await self._record_failure(session, task, error, now, profile)

    @staticmethod
    async def _current(session: AsyncSession, claimed: Task) -> Task | None:
        """仍持有本次领取的租约才保存结果；任务可能已被回收、补跑、取消或重新分析。"""
        task = await TaskRepository(session).get(Task, claimed.id, lock=True)
        if task and task.status == TaskStatus.RUNNING and task.lease_id == claimed.lease_id:
            return task
        return None

    async def _advance(
        self,
        session: AsyncSession,
        task: Task,
        output,
        profile: AiProfile | None,
        now: datetime,
    ) -> None:
        tasks, upstream = TaskRepository(session), UpstreamService(session)
        job = await JobRepository(session).require(Job, task.job_id, lock=True)
        generation = task.retry_generation
        match task.kind:
            case TaskKind.FETCH if isinstance(output, Job):
                task.succeed(now, {"reused_from": output.external_id})
                job.reuse(output, now)
            case TaskKind.FETCH:
                material = job.video.material
                task.succeed(
                    now,
                    {
                        "size": material.size,
                        "sha256": material.sha256,
                        "duration": output.duration,
                        "has_audio": output.has_audio,
                    },
                )
                job.fetched(output)
                job.analysis_key = await self._analysis_key(session, job)
                tasks.add(Task(TaskKind.EXTRACT_FRAMES, job.id, retry_generation=generation))
                if output.has_audio:
                    tasks.add(Task(TaskKind.EXTRACT_AUDIO, job.id, retry_generation=generation))
            case TaskKind.EXTRACT_FRAMES:
                job.frame_count = output
                task.succeed(now, _frames_result(job))
            case TaskKind.EXTRACT_AUDIO:
                job.segment_count = len(output)
                task.succeed(now, _audio_result(job, output, self._media.segment_seconds))
                for segment in range(len(output)):
                    tasks.add(Task(TaskKind.LISTEN, job.id, segment, retry_generation=generation))
            case TaskKind.LISTEN:
                task.succeed(now, output.model_dump(mode="json"))
            case TaskKind.RECOGNIZE:
                listening, recognition = output
                result = recognition.model_dump(mode="json")
                task.succeed(now, result)
                if await self._analysis_key(session, job, profile) != job.analysis_key:
                    # 分析期间切换了模型或提示词，这次结果不作为复用来源。
                    job.analysis_key = None
                job.complete(
                    {
                        "listening": listening.model_dump(mode="json"),
                        "recognition": {**result, "tags": recognition.display_tags(listening)},
                    },
                    now,
                )
            case TaskKind.SUMMARIZE:
                result = output.model_dump(mode="json")
                task.succeed(now, result)
                videos = [
                    {"job_id": video.job_id, "run": video.run} for video in job.summary.videos
                ]
                job.complete({**result, "videos": videos}, now)

        worker = await AttemptRepository(session).finish(task, now)
        await upstream.report_task(job, task, now, profile, worker=worker)
        if task.kind == TaskKind.FETCH and job.segment_count == 0:
            await upstream.report_skipped(
                job,
                TaskKind.EXTRACT_AUDIO,
                "视频没有音轨，不需要听音",
                now,
                worker=worker,
                retry_generation=generation,
            )
        if task.kind in PREPARATION and job.ready_for_recognition(
            len(await tasks.listen_results(job.id))
        ):
            tasks.add(Task(TaskKind.RECOGNIZE, job.id, retry_generation=generation))
        if job.status == JobStatus.COMPLETED:
            await self._settle(upstream, job, task, now, worker)

    async def _record_failure(
        self,
        session: AsyncSession,
        task: Task,
        error: Exception,
        now: datetime,
        profile: AiProfile | None = None,
    ) -> None:
        message = describe(error)
        if task.kind.uses_ai and isinstance(error, TimeoutError | WorkerLost):
            task.hold_for_review(message, now)
        else:
            task.fail(message, now, None if isinstance(error, Rejected) else self._retry)
        upstream = UpstreamService(session)
        job = await JobRepository(session).require(Job, task.job_id, lock=True)
        worker = await AttemptRepository(session).finish(task, now)
        await upstream.report_task(job, task, now, profile, worker=worker)
        if task.status == TaskStatus.READY:
            return
        if task.status == TaskStatus.NEEDS_REVIEW:
            if job.status == JobStatus.FAILED:
                return
            job.hold_for_review(message, now)
        else:
            job.fail(message, now)
        await self._settle(upstream, job, task, now, worker)

    async def _settle(
        self,
        upstream: UpstreamService,
        job: Job,
        task: Task,
        now: datetime,
        worker: WorkerIdentity | None,
    ) -> None:
        """任务结束：确定抽帧图片和音频段的保留期，回传结果。"""
        if job.kind == JobKind.VIDEO and not job.reused_from:
            job.artifacts_until = now + self._retention
        await upstream.report_job(
            job,
            now,
            worker=worker,
            attempt_id=task.lease_id,
            retry_generation=task.retry_generation,
        )

    async def _analysis_key(
        self, session: AsyncSession, job: Job, recognize: AiProfile | None = None
    ) -> str | None:
        """素材、输入、媒体参数和模型配置的摘要；任何一项不同，结果都不能复用。"""
        calls = CallService(session)
        listen = await calls.routed(TaskKind.LISTEN)
        recognize = recognize or await calls.routed(TaskKind.RECOGNIZE)
        if listen is None or recognize is None:
            return None
        video = job.video
        basis = {
            "sha256": video.material.sha256,
            "description": video.description,
            "topics": video.topics,
            "frame_count": self._media.frame_count,
            "segment_seconds": self._media.segment_seconds,
            "listen": listen.fingerprint,
            "recognize": recognize.fingerprint,
        }
        encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


def _artifact_url(job: Job, kind: str, index: int) -> str:
    """阶段产物在本服务接口上的路径，调度中心加上注册的地址访问。"""
    return f"/api/jobs/{quote(job.external_id, safe='')}/{kind}/{index}"


def _frames_result(job: Job) -> dict:
    count, duration = job.frame_count or 0, job.duration or 0
    frames = [
        {"index": i, "at": round(i * duration / count, 1), "url": _artifact_url(job, "frames", i)}
        for i in range(count)
    ]
    return {"frame_count": count, "frames": frames}


def _audio_result(job: Job, indices: list[int], segment_seconds: int) -> dict:
    duration = job.duration or 0
    segments = [
        {
            "index": i,
            "start": original * segment_seconds,
            "end": min((original + 1) * segment_seconds, duration),
            "url": _artifact_url(job, "audio", i),
        }
        for i, original in enumerate(indices)
    ]
    result: dict = {"segment_count": len(indices), "segments": segments}
    if not indices:
        result["skip_reason"] = "没有可听的音频片段"
    return result


def describe(error: Exception) -> str:
    return str(error) or type(error).__name__
