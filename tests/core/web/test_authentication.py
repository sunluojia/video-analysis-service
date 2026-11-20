import httpx
import pytest
from pydantic import SecretStr

from tests.factories import SERVICE_TOKEN, api_settings
from video_analysis.core.config.settings import ApiSettings
from video_analysis.entrypoints.api import create_app


@pytest.fixture
async def client():
    app = create_app(api_settings(database_url="postgresql+asyncpg://unused/unused"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token"}, {"Authorization": f"Basic {SERVICE_TOKEN}"}],
)
async def test_api_requires_the_service_token(client, headers):
    response = await client.get("/api/status", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_api_docs_stay_open(client):
    assert (await client.get("/openapi.json")).status_code == 200


def test_service_token_is_required():
    with pytest.raises(ValueError):
        ApiSettings(service_token=SecretStr("short"))
