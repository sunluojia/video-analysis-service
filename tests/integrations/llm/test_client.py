import json
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, sentinel
from uuid import uuid4

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import RateLimitError

from video_analysis.integrations.llm import langchain_client
from video_analysis.integrations.llm.langchain_client import LangChainAi
from video_analysis.modules.ai.call.models import BAILIAN_EXPLICIT_CACHE, AiProfile, Usage
from video_analysis.modules.analysis.job.results import CreatorTags, Listening, Recognition
from video_analysis.modules.scheduling.execution.ports import Answer, Rejected, VideoMaterial


def listen_profile(base_url: str) -> AiProfile:
    return AiProfile(
        model_id=uuid4(),
        provider="openai",
        model="test-model",
        base_url=base_url,
        credential_ref=None,
        params={},
        timeout_seconds=30,
        prompt="分析音频",
        prompt_id=None,
        prompt_version=None,
    )


async def test_summary_receives_product_evidence_and_separate_published_topics(monkeypatch):
    video = Recognition.model_validate(
        {
            "tags": ["卫衣", "产品展示", "#卫衣"],
            "summary": "展示卫衣吊牌",
            "products": [
                {
                    "name": "卫衣",
                    "evidence": "吊牌可见",
                    "frame_indices": [0],
                    "sizes": [{"value": "XL", "evidence": "吊牌标注 XL"}],
                }
            ],
            "topic_translations": [{"topic": "#hoodie", "translation": "卫衣"}],
        }
    )
    ask = AsyncMock(return_value=Answer(CreatorTags(tags=["卫衣", "卫衣XL码"])))
    client = LangChainAi()
    monkeypatch.setattr(client, "_ask", ask)

    await client.summarize(sentinel.profile, [video])

    [payload] = json.loads(ask.call_args.args[2]["text"])
    assert payload == {**video.model_dump(mode="json"), "tags": ["卫衣", "产品展示"]}
    assert video.tags == ["卫衣", "产品展示", "#卫衣"]


async def test_recognition_keeps_only_appearance_observations_with_a_frame(tmp_path, monkeypatch):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    observations = [
        {"frame_index": 0, "skin_tone": "白肤色", "evidence": "肤色在帧中可见"},
        {"frame_index": 1, "age_group": "55岁+", "evidence": "帧序号超出输入"},
        {"frame_index": 0, "evidence": "没有可判断的属性"},
    ]
    response = Recognition(tags=[], summary="", appearances=observations)
    client = LangChainAi()
    monkeypatch.setattr(client, "_ask", AsyncMock(return_value=Answer(response)))
    material = VideoMaterial([frame], Listening(sounds=[]), "", [])

    result = (await client.recognize(sentinel.profile, material)).value

    assert len(result.appearances) == 1
    assert result.appearance_tags() == ["白肤色"]


async def test_summary_retains_distinct_appearances_from_old_and_new_results(monkeypatch):
    client = LangChainAi()
    response = CreatorTags(tags=["生活记录", "白肤色", "白肤色", "55岁+"])
    monkeypatch.setattr(client, "_ask", AsyncMock(return_value=Answer(response)))
    old = Recognition(tags=["白肤色", "偏白肤色"], summary="")
    new = Recognition.model_validate(
        {
            "tags": ["白肤色"],
            "summary": "",
            "appearances": [
                {"frame_index": 0, "age_group": "25-34岁", "evidence": "清楚的正脸"},
            ],
        }
    )

    later = Recognition(tags=[], summary="")
    summary = (await client.summarize(sentinel.profile, [old, new, later])).value

    assert summary.tags == ["生活记录", "白肤色", "偏白肤色", "25-34岁"]


@pytest.mark.parametrize(
    ("base_url", "data", "audio_format"),
    [
        (
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "data:audio/mp3;base64,SUQz",
            "mp3",
        ),
        ("http://127.0.0.1:8095/v1", "SUQz", "mpeg"),
    ],
)
async def test_listen_sends_the_audio_format_expected_by_each_endpoint(
    tmp_path, monkeypatch, base_url, data, audio_format
):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3")
    profile = replace(
        listen_profile(base_url),
        model="qwen3.8-omni-flash",
        params={BAILIAN_EXPLICIT_CACHE: True},
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/completions")
        assert payload["messages"][0] == {"role": "system", "content": profile.prompt}
        assert payload["messages"][-1]["content"][-1] == {
            "type": "input_audio",
            "input_audio": {"data": data, "format": audio_format},
        }
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "Listening",
                "arguments": '{"sounds":["speech"],"transcript":"你好"}',
            },
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "tool_calls": [tool_call]},
                    }
                ],
            },
        )

    client = LangChainAi()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        model = ChatOpenAI(
            model=profile.model,
            api_key="test-key",
            base_url=base_url,
            http_async_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(client, "_model", lambda _: model)
        result = (await client.listen(profile, audio)).value

    assert result.transcript == "你好"


@pytest.mark.parametrize("explicit_cache", [False, True], ids=["switched-off", "default"])
async def test_bailian_uses_a_stable_prefix_and_reports_stream_usage(
    tmp_path, monkeypatch, caplog, explicit_cache
):
    profile = replace(
        listen_profile("https://dashscope.aliyuncs.com/compatible-mode/v1"),
        model="qwen3.8-flash",
        params={"streaming": True}
        if explicit_cache
        else {BAILIAN_EXPLICIT_CACHE: False, "streaming": True},
        prompt="固定的识别规则和标签词典",
        prompt_version=3,
    )
    assert profile.fingerprint == replace(profile, params={"streaming": True}).fingerprint
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        payload = json.loads(request.content)
        assert BAILIAN_EXPLICIT_CACHE not in payload
        assert payload["stream_options"] == {"include_usage": True}
        calls.append(payload)
        tool_call = {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "Recognition", "arguments": '{"tags":[],"summary":"识别完成"}'},
        }
        usage = {
            "prompt_tokens": 3200,
            "completion_tokens": 20,
            "total_tokens": 3220,
            "prompt_tokens_details": {"cached_tokens": 0 if len(calls) == 1 else 2048},
        }
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "tool_calls": [tool_call]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": usage},
        ]
        metadata = {"id": "test", "model": profile.model, "object": "chat.completion.chunk"}
        body = "".join(f"data: {json.dumps(metadata | chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200, text=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    caplog.set_level(logging.INFO, logger=langchain_client.__name__)
    client = LangChainAi()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:

        def init_model(model, *, model_provider, **options):
            return ChatOpenAI(
                model=model, api_key="test-key", http_async_client=http_client, **options
            )

        monkeypatch.setattr(langchain_client, "init_chat_model", init_model)
        answers = []
        for index in range(2):
            frame = tmp_path / f"{index}.jpg"
            frame.write_bytes(f"frame {index}".encode())
            video = VideoMaterial([frame], Listening(sounds=[]), f"视频 {index}", [])
            answers.append(await client.recognize(profile, video))

    assert [answer.value.summary for answer in answers] == ["识别完成"] * 2
    assert [answer.usage for answer in answers] == [Usage(3200, 20, 0), Usage(3200, 20, 2048)]
    first, second = calls
    expected_content = (
        [{"type": "text", "text": profile.prompt, "cache_control": {"type": "ephemeral"}}]
        if explicit_cache
        else profile.prompt
    )
    assert first["messages"][0] == second["messages"][0]
    assert first["messages"][0] == {"role": "system", "content": expected_content}
    assert json.dumps(first["tools"]) == json.dumps(second["tools"])
    assert first["messages"][1] != second["messages"][1]
    assert all("cache_control" not in block for block in second["messages"][1]["content"])
    mode = "explicit" if explicit_cache else "default"
    assert f"prompt_version=3 mode={mode} prompt_tokens=3200 cached_tokens=0" in caplog.text
    assert f"prompt_version=3 mode={mode} prompt_tokens=3200 cached_tokens=2048" in caplog.text


@pytest.mark.parametrize(("status", "error"), [(400, Rejected), (429, RateLimitError)])
async def test_only_requests_the_model_refuses_are_rejected(tmp_path, monkeypatch, status, error):
    """内容审核、空音频这类 4xx 重发结果相同；限流可以稍后重发。"""
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3")
    profile = listen_profile("http://127.0.0.1:8095/v1")
    body = {"error": {"message": "The audio is empty", "type": "invalid_request_error"}}
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    client = LangChainAi()
    async with httpx.AsyncClient(transport=transport) as http_client:
        model = ChatOpenAI(
            model=profile.model,
            api_key="test-key",
            base_url=profile.base_url,
            http_async_client=http_client,
            max_retries=0,
        )
        monkeypatch.setattr(client, "_model", lambda _: model)
        with pytest.raises(error, match="The audio is empty"):
            await client.listen(profile, audio)
