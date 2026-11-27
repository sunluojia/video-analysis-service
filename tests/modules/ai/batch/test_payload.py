import json
from uuid import uuid4

import pytest

from video_analysis.integrations.llm.batch_payload import (
    LINE_LIMIT,
    OversizedBatchLine,
    build_line,
    parse_result,
)
from video_analysis.modules.ai.call.models import AiProfile, Usage
from video_analysis.modules.analysis.job.results import CreatorTags, Listening
from video_analysis.modules.scheduling.execution.ports import Answer, Rejected, VideoMaterial
from video_analysis.modules.scheduling.task.models import TaskKind

URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def profile(**params) -> AiProfile:
    return AiProfile(uuid4(), "openai", "qwen3.8-flash", URL, None, params, 5, "prompt", None, None)


def test_request_uses_native_batch_fields_and_jpeg_data_url(tmp_path):
    jpg = tmp_path / "000.jpg"
    jpg.write_bytes(b"jpeg")
    material = VideoMaterial([jpg], Listening(sounds=[], transcript="brief"), "description", [])
    line = build_line(
        "frame-task",
        profile(
            temperature=0,
            extra_body={"enable_thinking": False, "thinking_budget": 50, "stream_options": {}},
            streaming=True,
            stream_usage=True,
            cache_control={"type": "ephemeral"},
        ),
        TaskKind.RECOGNIZE,
        material,
        80_000_000,
    )
    payload = json.loads(line.data)
    assert payload["custom_id"] == "frame-task"
    assert payload["url"] == "/v1/chat/completions" and payload["method"] == "POST"
    body = payload["body"]
    assert body["model"] == "qwen3.8-flash"
    assert body["enable_thinking"] is False and body["temperature"] == 0
    assert not set(body) & {
        "extra_body",
        "streaming",
        "stream_usage",
        "cache_control",
        "thinking_budget",
        "stream_options",
    }
    assert body["tool_choice"]["function"]["name"] == body["tools"][0]["function"]["name"]
    assert (
        body["messages"][1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,anBlZw=="
    )
    assert line.frame_count == 1


def test_oversized_jpeg_is_fallback_before_encoding(tmp_path):
    jpg = tmp_path / "000.jpg"
    jpg.write_bytes(b"a" * (LINE_LIMIT * 3 // 4))
    material = VideoMaterial([jpg], Listening(sounds=[], transcript=""), "", [])
    with pytest.raises(OversizedBatchLine):
        build_line("frame-task", profile(), TaskKind.RECOGNIZE, material, 80_000_000)


@pytest.mark.parametrize("format", ["tool", "json"])
def test_result_parses_structure_and_usage(format):
    result = {"tags": ["makeup"]}
    text = json.dumps(result)
    msg = (
        {"content": text}
        if format == "json"
        else {"tool_calls": [{"function": {"name": "CreatorTags", "arguments": text}}]}
    )
    row = {
        "custom_id": "summary",
        "response": {
            "status_code": 200,
            "body": {
                "choices": [{"message": msg}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            },
        },
    }
    assert parse_result(TaskKind.SUMMARIZE, row) == Answer(CreatorTags(**result), Usage(12, 5, 3))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"error": {"code": "DataInspectionFailed", "message": "hidden"}}, Rejected),
        ({"response": {"status_code": 400, "body": {"error": {"message": "hidden"}}}}, Rejected),
        (
            {"response": {"status_code": 429, "body": {"error": {"message": "hidden"}}}},
            RuntimeError,
        ),
        ({"response": {"status_code": 200, "body": {"choices": []}}}, RuntimeError),
        ({"response": []}, RuntimeError),
    ],
)
def test_failures_do_not_echo_provider_bodies(row, expected):
    result = parse_result(TaskKind.RECOGNIZE, row)
    assert isinstance(result, expected)
    assert "hidden" not in str(result)
