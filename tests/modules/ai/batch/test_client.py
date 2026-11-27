import json

import httpx
from openai import AsyncOpenAI

from tests.modules.ai.batch.test_payload import profile
from video_analysis.integrations.llm import batch_client


async def test_sdk_upload_submit_poll_and_read_results(monkeypatch):
    requested = []
    batch = {
        "id": "remote-job",
        "object": "batch",
        "completion_window": "24h",
        "created_at": 1,
        "endpoint": "/v1/chat/completions",
        "input_file_id": "input-file",
        "status": "validating",
    }

    async def provider(request):
        path = request.url.path.removeprefix("/compatible-mode/v1")
        requested.append((request.method, path))
        if path == "/files":
            payload = await request.aread()
            assert b'name="purpose"' in payload and b"\r\nbatch\r\n" in payload
            assert b'filename="job.jsonl"' in payload
            return httpx.Response(200, json={"id": "input-file"})
        if path == "/batches":
            assert json.loads(await request.aread()) == {
                "completion_window": "24h",
                "endpoint": "/v1/chat/completions",
                "input_file_id": "input-file",
            }
            return httpx.Response(200, json=batch)
        if path == "/batches/remote-job":
            return httpx.Response(
                200, json={**batch, "status": "completed", "output_file_id": "output-file"}
            )
        assert path == "/files/output-file/content"
        return httpx.Response(
            200, content=b'{"custom_id":"task","error":null}\ninvalid JSON\n{"missing":"id"}\n'
        )

    def use_mock(**options):
        return AsyncOpenAI(
            **{**options, "api_key": "test-only"},
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        )

    monkeypatch.setattr(batch_client, "AsyncOpenAI", use_mock)
    async with batch_client.BatchClient(profile()) as client:
        file_id = await client.upload("job.jsonl", b"{}\n")
        assert (await client.submit(file_id)).status == "validating"
        remote = await client.retrieve("remote-job")
        assert remote.status == "completed"
        assert await client.results(remote.output_file_id) == {
            "task": {"custom_id": "task", "error": None}
        }
    assert requested == [
        ("POST", "/files"),
        ("POST", "/batches"),
        ("GET", "/batches/remote-job"),
        ("GET", "/files/output-file/content"),
    ]
