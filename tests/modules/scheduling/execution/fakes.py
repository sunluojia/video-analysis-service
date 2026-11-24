import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace
from video_analysis.modules.ai.call.models import AiProfile, Usage
from video_analysis.modules.analysis.job.models import Material, MediaInfo
from video_analysis.modules.analysis.job.results import CreatorTags, Listening, Recognition, Sound
from video_analysis.modules.scheduling.execution.pipeline import Pipeline
from video_analysis.modules.scheduling.execution.ports import (
    Answer,
    Materials,
    Media,
    Rejected,
    VideoAi,
    VideoMaterial,
)
from video_analysis.modules.scheduling.execution.worker import Worker, WorkerLimits
from video_analysis.modules.scheduling.task.models import RetryPolicy, WorkerIdentity

NO_RETRY = RetryPolicy(max_attempts=1)
USAGE = Usage(input_tokens=2000, output_tokens=100, cached_tokens=1500)


class FakeMaterials(Materials):
    """素材 ID 就是文件内容。关闭 gate 时下载停住，视频保持处理中。"""

    def __init__(self) -> None:
        self.downloads = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def download(self, material: Material, target: Path) -> None:
        self.downloads += 1
        await self.gate.wait()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(material.id.encode())


class FakeMedia(Media):
    """每个视频抽 3 张图、切 segments 段音频，写出空文件；segments 为 0 表示数字静音。"""

    def __init__(self, segments: int = 2, *, has_audio: bool = True) -> None:
        self.segments = segments
        self.has_audio = has_audio
        self.frame_count = 3
        self.segment_seconds = 10

    async def probe(self, source: Path) -> MediaInfo:
        assert source.is_file()
        return MediaInfo(duration=30, has_audio=self.has_audio)

    async def extract_frames(self, source: Path, target: Path) -> int:
        for index in range(self.frame_count):
            (target / f"{index:03d}.jpg").write_bytes(b"jpg")
        return self.frame_count

    async def extract_audio(self, source: Path, target: Path) -> int:
        for index in range(self.segments):
            (target / f"{index:03d}.mp3").write_bytes(b"mp3")
        return self.segments


class FakeAi(VideoAi):
    """用发布描述作为识别标签；记录用到的模型、提示词和同时在途的请求数。

    关闭 gate 时请求停在模型里，用来让在途请求保持占用额度。rejected 里的描述会被模型拒绝。
    """

    def __init__(self, *, slow: Collection[str] = (), rejected: Collection[str] = ()) -> None:
        self.slow = set(slow)
        self.rejected = set(rejected)
        self.gate = asyncio.Event()
        self.gate.set()
        self.models: list[str] = []
        self.prompts: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def listen(self, profile: AiProfile, audio: Path) -> Answer[Listening]:
        await self._call(profile)
        return Answer(Listening(sounds=[Sound.SPEECH], transcript=f"第{audio.stem}段"), USAGE)

    async def recognize(self, profile: AiProfile, material: VideoMaterial) -> Answer[Recognition]:
        if material.description in self.slow:
            self.slow.remove(material.description)
            await asyncio.sleep(profile.timeout_seconds * 10)
        await self._call(profile)
        if material.description in self.rejected:
            raise Rejected("Error code: 400 - Input image data may contain inappropriate content")
        return Answer(
            Recognition(tags=[material.description], summary=material.listening.transcript), USAGE
        )

    async def summarize(
        self, profile: AiProfile, videos: Sequence[Recognition]
    ) -> Answer[CreatorTags]:
        await self._call(profile)
        return Answer(CreatorTags(tags=[tag for video in videos for tag in video.tags]), USAGE)

    async def _call(self, profile: AiProfile) -> None:
        self.models.append(profile.model)
        self.prompts.append(profile.prompt)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.gate.wait()
        finally:
            self.in_flight -= 1


def in_flight(ai: FakeAi, count: int) -> Callable[[], Awaitable[bool]]:
    async def reached() -> bool:
        return ai.in_flight == count

    return reached


class Setup:
    """一套测试用的执行环境：同一个工作目录、素材来源、媒体处理和模型。"""

    def __init__(
        self,
        database: Database,
        workspace: Workspace,
        *,
        ai: FakeAi | None = None,
        media: FakeMedia | None = None,
        retention: timedelta = timedelta(hours=1),
        retry: RetryPolicy = NO_RETRY,
    ) -> None:
        self.database = database
        self.workspace = workspace
        self.materials = FakeMaterials()
        self.media = media or FakeMedia()
        self.ai = ai or FakeAi()
        self.pipeline = Pipeline(
            database,
            workspace,
            self.materials,
            self.media,
            self.ai,
            retry,
            timedelta(seconds=5),
            retention,
        )

    def worker(
        self,
        *,
        admission_factor: float = 10,
        backlog: int = 10,
        identity: WorkerIdentity | None = None,
        artifact_cleanup_enabled: bool = True,
    ) -> Worker:
        limits = WorkerLimits(
            download_slots=4,
            media_slots=2,
            admission_factor=admission_factor,
            media_backlog=backlog,
            min_free_bytes=0,
            listen_per_video=2,
            media_timeout=timedelta(seconds=5),
            poll_interval=0.05,
            artifact_cleanup_enabled=artifact_cleanup_enabled,
        )
        return Worker(
            self.database,
            self.pipeline,
            self.workspace,
            limits,
            identity or WorkerIdentity("test-worker", uuid4()),
        )

    @asynccontextmanager
    async def running(self, *, workers: int = 1, **limits) -> AsyncIterator[None]:
        """运行真实的 Worker 循环。退出时放行停住的请求，停止领取并等已开始的任务结束。"""
        instances = [self.worker(**limits) for _ in range(workers)]
        loops = [asyncio.create_task(worker.run()) for worker in instances]
        try:
            yield
        finally:
            self.ai.gate.set()
            self.materials.gate.set()
            for worker in instances:
                worker.stop()
            await asyncio.gather(*loops)

    async def run_until(self, done: Callable[[], Awaitable[bool]], **limits) -> None:
        async with self.running(**limits):
            await wait_until(done)


async def wait_until(done: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(10):
        while not await done():
            await asyncio.sleep(0.05)
