"""HTTP 接口：AI 配置、任务查询与控制、运行状态。启动后向调度中心注册并保持心跳。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version

import httpx
import uvicorn
from fastapi import Depends, FastAPI

from video_analysis.core.config.logging_setup import configure_logging
from video_analysis.core.config.settings import ApiSettings
from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace
from video_analysis.core.web.authentication import ServiceToken
from video_analysis.core.web.exception_handlers import register_exception_handlers
from video_analysis.integrations.scheduler.client import SchedulerClient
from video_analysis.modules.ai.call.router import router as ai_usage_router
from video_analysis.modules.ai.config.router import router as ai_config_router
from video_analysis.modules.ai.prompt.router import router as prompt_router
from video_analysis.modules.analysis.batch.router import router as batch_router
from video_analysis.modules.analysis.job.router import router as job_router
from video_analysis.modules.scheduling.status.router import router as status_router
from video_analysis.modules.scheduling.task.router import router as control_router

ROUTERS = (
    job_router,
    control_router,
    batch_router,
    status_router,
    ai_config_router,
    ai_usage_router,
    prompt_router,
)
VERSION = version("video-analysis-service")


def create_app(settings: ApiSettings) -> FastAPI:
    token = settings.service_token.get_secret_value()
    database = Database(settings.database_url)
    scheduler = SchedulerClient(settings.scheduler_url, token)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        heartbeat = asyncio.create_task(
            scheduler.keep_registered(
                settings.instance_id, settings.public_url, VERSION, settings.heartbeat_seconds
            )
        )
        yield
        heartbeat.cancel()
        with suppress(httpx.HTTPError):
            await scheduler.withdraw(settings.instance_id)
        await scheduler.aclose()
        await database.dispose()

    # 全部接口都要求服务凭据；接口文档不含数据，保持开放。
    app = FastAPI(
        title="视频分析服务",
        version=VERSION,
        lifespan=lifespan,
        dependencies=[Depends(ServiceToken(token))],
    )
    app.state.settings = settings
    app.state.database = database
    app.state.workspace = Workspace(settings.work_root)
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    register_exception_handlers(app)
    return app


def main() -> None:
    # noinspection PyArgumentList
    settings = ApiSettings()  # 服务凭据从环境变量读取
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
