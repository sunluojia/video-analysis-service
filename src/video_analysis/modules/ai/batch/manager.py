import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from openai import NotFoundError
from openai.types import Batch

from video_analysis.core.common.clock import utcnow
from video_analysis.core.common.errors import NotFound
from video_analysis.core.database.engine import Database
from video_analysis.integrations.llm.batch_client import BatchClient, describe_batch_error
from video_analysis.integrations.llm.batch_payload import (
    OversizedBatchLine,
    RequestLine,
    build_line,
    parse_result,
    provider_error,
    safe_code,
)
from video_analysis.modules.ai.batch.models import TERMINAL, BatchItem, BatchJob
from video_analysis.modules.ai.batch.repository import BatchJobRepository
from video_analysis.modules.ai.call.models import AiProfile
from video_analysis.modules.ai.call.service import CallService
from video_analysis.modules.ai.config.models import AiModel
from video_analysis.modules.ai.config.repository import AiConfigRepository
from video_analysis.modules.ai.quota.repository import QuotaRepository
from video_analysis.modules.scheduling.execution.claims import begin_task
from video_analysis.modules.scheduling.execution.pipeline import Pipeline
from video_analysis.modules.scheduling.execution.ports import Rejected
from video_analysis.modules.scheduling.task.models import Task, TaskKind, TaskStatus, WorkerIdentity
from video_analysis.modules.scheduling.task.repository import TaskRepository

log = logging.getLogger(__name__)
LEASE = timedelta(hours=25)
SUBMIT_LOOKUP_GRACE = timedelta(minutes=1)
SUBMIT_LOOKUP_LIMIT = timedelta(minutes=5)


@dataclass(frozen=True)
class BatchLimits:
    max_items: int
    max_bytes: int
    flush_after: timedelta
    poll_interval: timedelta
    max_active: int


class BatchManager:
    def __init__(
        self,
        database: Database,
        pipeline: Pipeline,
        limits: BatchLimits,
        identity: WorkerIdentity,
        client_factory: Callable[[AiProfile], BatchClient] = BatchClient,
    ) -> None:
        self._database = database
        self._pipeline = pipeline
        self._limits = limits
        self._identity = identity
        self._client = client_factory
        self._stop = asyncio.Event()
        self._poll_after = utcnow()
        self._submit_after = utcnow()

    async def run(self) -> None:
        interval = min(5.0, self._limits.flush_after.total_seconds())
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                log.error("Bailian Batch scheduler tick failed (%s)", type(error).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> None:
        # The advisory lock does not block real-time claims or keep task rows locked over HTTP.
        async with self._database.transaction() as lock:
            if not await BatchJobRepository(lock).acquire_manager():
                return
            now = utcnow()
            if now >= self._poll_after:
                await self._poll(now)
                self._poll_after = now + self._limits.poll_interval
            if not self._stop.is_set() and now >= self._submit_after:
                await self._pack(now)

    async def _pack(self, now: datetime) -> None:
        async with self._database.transaction() as session:
            remaining = self._limits.max_active - len(await BatchJobRepository(session).active())
            if remaining <= 0:
                return
            groups = []
            tasks = TaskRepository(session)
            for pool in await QuotaRepository(session).pools():
                for kind in pool.batch_kinds:
                    candidates = await tasks.batch_candidates(kind, now, self._limits.max_items)
                    if candidates:
                        groups.append((candidates[0].run_after, pool.model_id, kind, candidates))
        for _, model_id, kind, candidates in sorted(groups):
            if self._stop.is_set() or remaining <= 0:
                break
            if await self._pack_group(now, model_id, kind, candidates):
                remaining -= 1
            if now < self._submit_after:
                break

    async def _pack_group(
        self, now: datetime, model_id: UUID, kind: TaskKind, candidates: list[Task]
    ) -> bool:
        async with self._database.transaction() as session:
            profile = await CallService(session).routed(kind)
            if profile is None or profile.model_id != model_id:
                return False
        lines: dict[UUID, RequestLine] = {}
        fallbacks = []
        size = 0
        full = False
        oldest = None
        for task in candidates:
            try:
                material = await self._pipeline.prepare_ai(task)
                line = await asyncio.to_thread(
                    build_line, str(uuid4()), profile, kind, material, self._limits.max_bytes
                )
            except (NotFound, OSError, OversizedBatchLine, Rejected):
                fallbacks.append(task.id)
                continue
            if size + len(line.data) > self._limits.max_bytes:
                full = True
                break
            size += len(line.data)
            lines[task.id] = line
            oldest = oldest or task.run_after
        if fallbacks:
            async with self._database.transaction() as session:
                await TaskRepository(session).fallback_batch(fallbacks)
        if oldest is None or not (
            full
            or size >= self._limits.max_bytes
            or len(lines) >= self._limits.max_items
            or now - oldest >= self._limits.flush_after
        ):
            return False
        if self._stop.is_set():
            return False
        claim = await self._claim(now, kind, profile, lines)
        if claim is None:
            return False
        job, payload = claim
        await self._submit(job, payload)
        return True

    async def _claim(
        self, now: datetime, kind: TaskKind, profile: AiProfile, lines: dict[UUID, RequestLine]
    ) -> tuple[BatchJob, bytes] | None:
        async with self._database.transaction() as session:
            tasks = await TaskRepository(session).batch_candidates(
                kind, now, len(lines), claim_ids=lines.keys()
            )
            # Recheck both the batch mode and routing after reading local files.
            if not tasks or await CallService(session).routed(kind) != profile:
                return None
            model = await AiConfigRepository(session).require(AiModel, profile.model_id)
            snapshot = asdict(profile)
            snapshot.pop("model_id")
            snapshot.pop("model")
            if profile.prompt_id:
                snapshot["prompt_id"] = str(profile.prompt_id)
            job = BatchJob(
                uuid4(),
                model.provider_id,
                model.id,
                model.name,
                kind,
                snapshot,
                len(tasks),
                now,
                now,
            )
            repository = BatchJobRepository(session)
            repository.add(job)
            for line_number, task in enumerate(tasks, 1):
                await begin_task(session, task, now, LEASE, self._identity, profile, ai_batch=True)
                assert task.lease_id is not None
                line = lines[task.id]
                repository.add(
                    BatchItem(
                        line.custom_id,
                        job.id,
                        task.id,
                        task.job_id,
                        task.lease_id,
                        profile.model_id,
                        profile.prompt_version,
                        line.frame_count,
                        line_number,
                        now,
                    )
                )
        return job, b"".join(lines[task.id].data for task in tasks)

    async def _submit(self, job: BatchJob, payload: bytes) -> None:
        try:
            client = self._client(job.profile)
        except Exception as error:
            await self._submission_failed(job, describe_batch_error(error, "setup"))
            return
        async with client:
            try:
                file_id = await client.upload(f"analysis-{job.id}.jsonl", payload)
            except Exception as error:
                await self._submission_failed(job, describe_batch_error(error, "upload"))
                return
            await self._update(job.id, remote_file_id=file_id)
            try:
                remote = await client.submit(file_id)
            except Exception as error:
                failure = describe_batch_error(error, "submission")
                if isinstance(failure, Rejected):
                    await self._submission_failed(job, failure)
                else:
                    # A POST with no reply may still have created a job. Poll by input_file_id.
                    self._submit_after = utcnow() + self._limits.poll_interval
                    await self._update(job.id, error=str(failure))
                return
            await self._record_remote(job.id, remote)
            log.info(
                "Bailian Batch submitted %s (%s, %d items)",
                job.id,
                job.task_kind,
                job.request_count,
            )

    async def _submission_failed(self, job: BatchJob, error: Exception) -> None:
        self._submit_after = utcnow() + self._limits.poll_interval
        status = "rejected" if isinstance(error, Rejected) else "failed"
        await self._update(job.id, status=status, error=str(error))
        await self._settle(job, {}, error)

    async def _poll(self, now: datetime) -> None:
        async with self._database.transaction() as session:
            jobs = await BatchJobRepository(session).active()
        for job in jobs:
            if self._stop.is_set():
                return
            await self._poll_job(job, now)

    async def _poll_job(self, job: BatchJob, now: datetime) -> None:
        if job.status == "rejected":
            await self._settle(job, {}, Rejected(job.error or "Bailian Batch rejected the request"))
            return
        if not job.remote_file_id:
            error = RuntimeError(job.error or "Bailian Batch upload interrupted")
            await self._update(job.id, status="failed", error=str(error))
            await self._settle(job, {}, error)
            return
        deadline = job.created_at + (LEASE if job.remote_batch_id else SUBMIT_LOOKUP_LIMIT)
        try:
            async with self._client(job.profile) as client:
                if job.remote_batch_id:
                    remote = await client.retrieve(job.remote_batch_id)
                else:
                    remote = await client.find(job.remote_file_id, job.created_at)
                if remote is None:
                    if now >= job.updated_at + SUBMIT_LOOKUP_GRACE:
                        await self._update(job.id, status="failed")
                        await self._settle(
                            job,
                            {},
                            RuntimeError(job.error or "Bailian Batch submission interrupted"),
                        )
                    return
                deadline = job.created_at + LEASE
                await self._record_remote(job.id, remote)
                if remote.status not in TERMINAL and now < deadline:
                    return
                rows = await self._download_results(client, remote)
        except Exception as error:
            failure = describe_batch_error(error, "poll")
            if now >= deadline or isinstance(failure, Rejected):
                await self._update(job.id, status="failed", error=str(failure))
                await self._settle(job, {}, failure)
            else:
                log.warning("Bailian Batch %s: %s", job.id, failure)
            return
        if remote.status not in TERMINAL:
            await self._update(job.id, status="expired", error="Bailian Batch settlement deadline")
        line_errors = {
            error.line: provider_error(error.code)
            for error in (remote.errors.data or [] if remote.errors else [])
            if error.line is not None
        }
        await self._settle(job, rows, self._missing_result(remote), line_errors=line_errors)

    @staticmethod
    async def _download_results(client: BatchClient, remote: Batch) -> dict[str, dict]:
        rows = {}
        for file_id in (remote.error_file_id, remote.output_file_id):
            if file_id:
                try:
                    rows.update(await client.results(file_id))
                except NotFoundError:
                    # Expired or missing results are handled by custom ID during settlement.
                    continue
        return rows

    @staticmethod
    def _missing_result(remote: Batch) -> Exception:
        if remote.errors:
            for error in remote.errors.data or []:
                if error.line is None:
                    return provider_error(error.code)
        if remote.status == "completed":
            return RuntimeError("Bailian Batch completed without a result for this request")
        if remote.status in TERMINAL:
            return RuntimeError(f"Bailian Batch ended with status {remote.status}")
        return RuntimeError("Bailian Batch settlement deadline reached")

    async def _record_remote(self, job_id: UUID, remote: Batch) -> None:
        counts = remote.request_counts
        code = remote.errors.data[0].code if remote.errors and remote.errors.data else None
        await self._update(
            job_id,
            remote_batch_id=remote.id,
            status=remote.status,
            output_file_id=remote.output_file_id,
            error_file_id=remote.error_file_id,
            completed_count=counts.completed if counts else 0,
            failed_count=counts.failed if counts else 0,
            error=safe_code(code),
        )

    async def _settle(
        self,
        job: BatchJob,
        rows: dict[str, dict],
        missing: Exception,
        *,
        line_errors: dict[int, Exception] | None = None,
    ) -> None:
        async with self._database.transaction() as session:
            items = await BatchJobRepository(session).pending_items(job.id)
        profile = job.profile
        for item in items:
            async with self._database.transaction() as session:
                task = await TaskRepository(session).get(Task, item.task_id)
            row = rows.get(item.custom_id)
            failure = (line_errors or {}).get(item.line_number, missing)
            result = parse_result(job.task_kind, row) if row is not None else failure
            error = str(result) if isinstance(result, Exception) else None
            if task is None or task.lease_id != item.lease_id:
                status = "superseded"
            elif task.status == TaskStatus.RUNNING:
                settled = await self._pipeline.settle_batch(task, profile, result, item.frame_count)
                status = ("failed" if error else "succeeded") if settled else "superseded"
            else:
                # The pipeline may have committed just before a previous poller stopped.
                status = "succeeded" if task.status == TaskStatus.SUCCEEDED else "failed"
            async with self._database.transaction() as session:
                saved = await BatchJobRepository(session).require(BatchItem, item.custom_id)
                saved.status = status
                saved.error = error if status == "failed" else None
                saved.finished_at = utcnow()
        await self._update(job.id, finished_at=utcnow())

    async def _update(self, job_id: UUID, **values) -> None:
        async with self._database.transaction() as session:
            job = await BatchJobRepository(session).require(BatchJob, job_id)
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = utcnow()
