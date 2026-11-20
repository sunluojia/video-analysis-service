import os
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.factories import SERVICE_TOKEN, api_settings
from video_analysis.core.database.engine import Database
from video_analysis.core.storage.workspace import Workspace
from video_analysis.entrypoints.api import create_app
from video_analysis.modules.tables import metadata

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def database_url() -> str:
    """数据库测试使用独立的测试库，每次运行都会重建表结构。"""
    url = os.environ.get("VA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("设置 VA_TEST_DATABASE_URL 后运行数据库测试")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def database(database_url: str):
    database = Database(database_url)
    tables = ", ".join(table.name for table in metadata.sorted_tables)
    async with database.engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables}"))
    yield database
    await database.dispose()


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace(tmp_path / "work")


@pytest.fixture
async def client(database_url: str, database: Database, tmp_path):
    """不运行 lifespan，测试不向调度中心注册。"""
    app = create_app(api_settings(database_url=database_url, work_root=tmp_path / "work"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    ) as client:
        yield client
