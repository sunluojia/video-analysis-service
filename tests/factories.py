"""测试共用的配置、数据准备与结果读取。"""

import hashlib
from collections.abc import Awaitable, Callable

from pydantic import SecretStr
from sqlalchemy import select

from video_analysis.core.config.settings import ApiSettings
from video_analysis.core.database.engine import Database
from video_analysis.core.messaging.outbox import outbox
from video_analysis.modules.ai.config.models import AiModel, AiProvider
from video_analysis.modules.ai.config.repository import AiConfigRepository
from video_analysis.modules.ai.config.service import AiConfigService
from video_analysis.modules.analysis.job.models import Job
from video_analysis.modules.analysis.job.repository import JobRepository
from video_analysis.modules.analysis.job.results import Recognition
from video_analysis.modules.analysis.upstream.messages import AnalyzeVideo, SummarizeCreator
from video_analysis.modules.analysis.upstream.service import UpstreamService
from video_analysis.modules.scheduling.task.models import AI_KINDS

CONTENT = b"fake video"
SERVICE_TOKEN = "test-service-token"


def api_settings(**values) -> ApiSettings:
    return ApiSettings(service_token=SecretStr(SERVICE_TOKEN), **values)


def video_message(
    job_id: str = "j-1",
    description: str = "美妆",
    *,
    run: int = 1,
    batch: str = "b-1",
    content: bytes = CONTENT,
    allow_reuse: bool = True,
) -> AnalyzeVideo:
    """素材内容决定大小和 SHA-256；FakeMaterials 按 ID 写出同样的内容。"""
    material = {
        "id": content.decode(),
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    return AnalyzeVideo.model_validate(
        {
            "job_id": job_id,
            "run": run,
            "batch_id": batch,
            "creator_id": "c-1",
            "material": material,
            "description": description,
            "allow_reuse": allow_reuse,
        }
    )


def summary_message(*tags: str, job_id: str = "s-1") -> SummarizeCreator:
    videos = [
        {"job_id": f"j-{index}", "run": 1, "recognition": Recognition(tags=[tag], summary="")}
        for index, tag in enumerate(tags)
    ]
    return SummarizeCreator.model_validate(
        {"job_id": job_id, "run": 1, "batch_id": "b-1", "creator_id": "c-1", "videos": videos}
    )


async def accept(database: Database, message: AnalyzeVideo | SummarizeCreator) -> None:
    async with database.transaction() as session:
        upstream = UpstreamService(session)
        if isinstance(message, AnalyzeVideo):
            await upstream.accept_video(message)
        else:
            await upstream.accept_summary(message)


async def configure_ai(database: Database, *, model="test-model", capacity=10, timeout=5.0) -> None:
    """添加一个模型并让全部 AI 任务改用它；再次调用即切换路由。"""
    async with database.transaction() as session:
        config = AiConfigRepository(session)
        provider = AiProvider(model, "openai")
        ai_model = AiModel(provider.id, model, capacity, 1000, timeout)
        config.add(provider)
        config.add(ai_model)
        for kind in AI_KINDS:
            await AiConfigService(session).save_route(kind, ai_model.id, share=1)


async def load_job(database: Database, job_id: str = "j-1") -> Job:
    async with database.transaction() as session:
        return await JobRepository(session).require_external(job_id)


async def events(database: Database, **matching) -> list[dict]:
    """写入发件箱的事件，按写入顺序；可按字段筛选。"""
    async with database.engine.connect() as connection:
        rows = await connection.execute(select(outbox.c.payload).order_by(outbox.c.id))
    return [
        event
        for event in rows.scalars()
        if all(event.get(key) == value for key, value in matching.items())
    ]


def emitted(database: Database, **matching) -> Callable[[], Awaitable[bool]]:
    async def found() -> bool:
        return bool(await events(database, **matching))

    return found
