import asyncio
import hashlib
import json

import httpx
import pytest

from video_analysis.integrations.scheduler.client import SchedulerClient
from video_analysis.modules.analysis.job.models import Material

CONTENT = b"0123456789" * 100


def material(content: bytes = CONTENT) -> Material:
    return Material(id="m/1", size=len(content), sha256=hashlib.sha256(content).hexdigest())


def serving(content: bytes, requests: list[httpx.Request]) -> httpx.MockTransport:
    """按 Range 返回素材的一部分。"""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        status = 206 if start else 200
        return httpx.Response(status, content=content[start:])

    return httpx.MockTransport(handle)


async def test_download_resumes_and_verifies(tmp_path):
    requests: list[httpx.Request] = []
    target = tmp_path / "job" / "source"
    target.parent.mkdir()
    (tmp_path / "job" / "source.part").write_bytes(CONTENT[:300])
    client = SchedulerClient("http://scheduler", "token", serving(CONTENT, requests))

    await client.download(material(), target)

    assert target.read_bytes() == CONTENT
    assert not (tmp_path / "job" / "source.part").exists()
    [request] = requests
    assert request.url.raw_path == b"/api/materials/m%2F1"
    assert request.headers["Range"] == "bytes=300-"
    assert request.headers["Authorization"] == "Bearer token"


async def test_mismatched_download_is_discarded(tmp_path):
    client = SchedulerClient("http://scheduler", "token", serving(b"x" * len(CONTENT), []))
    target = tmp_path / "source"

    with pytest.raises(ValueError):
        await client.download(material(), target)

    assert not target.exists()
    assert not (tmp_path / "source.part").exists()


async def test_registration_keeps_trying_until_the_scheduler_is_up():
    requests: list[httpx.Request] = []
    registered = asyncio.Event()

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        registered.set()
        return httpx.Response(204)

    client = SchedulerClient("http://scheduler", "token", httpx.MockTransport(handle))
    heartbeat = asyncio.create_task(
        client.keep_registered("host-1", "http://analysis:8000", "1.0", interval=0.01)
    )
    async with asyncio.timeout(5):
        await registered.wait()
    heartbeat.cancel()

    assert {(request.method, request.url.path) for request in requests} == {
        ("PUT", "/api/analysis-services/host-1")
    }
    assert json.loads(requests[0].content) == {"url": "http://analysis:8000", "version": "1.0"}
