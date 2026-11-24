"""分析 Worker：领取各阶段任务并执行，同时把事件投递给调度中心。可以启动多个实例。"""

import asyncio
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import timedelta

from video_analysis.core.config.logging_setup import configure_logging
from video_analysis.core.config.settings import WorkerSettings
from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace
from video_analysis.integrations.llm.langchain_client import LangChainAi
from video_analysis.integrations.media.ffmpeg import FfmpegMedia, missing_tools, not_found
from video_analysis.integrations.mq.broker import create_broker
from video_analysis.integrations.mq.publisher import EventPublisher
from video_analysis.integrations.scheduler.client import SchedulerClient
from video_analysis.modules.scheduling.execution.pipeline import Pipeline
from video_analysis.modules.scheduling.execution.worker import Worker, WorkerLimits
from video_analysis.modules.scheduling.task.models import RetryPolicy, WorkerIdentity

log = logging.getLogger(__name__)

GB = 1024**3


async def run(settings: WorkerSettings) -> None:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(settings.thread_pool_size))
    database = Database(settings.database_url)
    workspace = Workspace(settings.work_root)
    scheduler = SchedulerClient(settings.scheduler_url, settings.service_token.get_secret_value())
    media_timeout = timedelta(seconds=settings.media_timeout_seconds)
    pipeline = Pipeline(
        database,
        workspace,
        scheduler,
        FfmpegMedia(
            frame_count=settings.frame_count, segment_seconds=settings.audio_segment_seconds
        ),
        LangChainAi(),
        RetryPolicy(settings.max_attempts, timedelta(seconds=settings.retry_delay_seconds)),
        media_timeout,
        timedelta(hours=settings.artifact_retention_hours),
    )
    limits = WorkerLimits(
        download_slots=settings.download_slots,
        media_slots=settings.media_slots,
        admission_factor=settings.admission_factor,
        media_backlog=settings.media_backlog,
        min_free_bytes=int(settings.min_free_disk_gb * GB),
        listen_per_video=settings.listen_per_video,
        media_timeout=media_timeout,
        poll_interval=settings.poll_interval_seconds,
        artifact_cleanup_enabled=settings.artifact_cleanup_enabled,
    )
    identity = WorkerIdentity(settings.worker_id, settings.worker_session_id)
    worker = Worker(database, pipeline, workspace, limits, identity)
    publisher = EventPublisher(
        database, create_broker(settings), settings.event_queue, settings.event_actor
    )
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        # Windows 不支持信号处理器，退出时由租约回收未完成的任务。
        with suppress(NotImplementedError):
            loop.add_signal_handler(stop_signal, worker.stop)
    publishing = asyncio.create_task(publisher.run(settings.poll_interval_seconds))
    try:
        await worker.run()
    finally:
        publishing.cancel()
        await scheduler.aclose()
        await database.dispose()


def main() -> None:
    # noinspection PyArgumentList
    settings = WorkerSettings()  # 服务凭据从环境变量读取
    configure_logging(settings.log_level)
    if missing := missing_tools():
        # 不退出：仍要投递事件、执行汇总；视频在媒体阶段失败，并把原因回传调度中心。
        log.warning("%s；在此之前视频分析都会失败", not_found(*missing))
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
