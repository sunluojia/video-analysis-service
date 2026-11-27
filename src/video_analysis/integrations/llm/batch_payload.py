import base64
import json
from dataclasses import dataclass

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from video_analysis.integrations.llm.langchain_client import RETRYABLE_STATUS
from video_analysis.modules.ai.call.formatting import recognition_prompt, summary_prompt
from video_analysis.modules.ai.call.models import AiProfile, Usage
from video_analysis.modules.analysis.job.results import CreatorTags, Recognition
from video_analysis.modules.scheduling.execution.ports import Answer, Rejected, VideoMaterial
from video_analysis.modules.scheduling.task.models import TaskKind

CHAT_COMPLETIONS = "/v1/chat/completions"
LINE_LIMIT = 900 * 1024
SCHEMAS = {TaskKind.RECOGNIZE: Recognition, TaskKind.SUMMARIZE: CreatorTags}
PARAMETERS = frozenset(
    {
        "enable_thinking",
        "frequency_penalty",
        "max_completion_tokens",
        "max_tokens",
        "presence_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "temperature",
        "thinking_budget",
        "top_k",
        "top_p",
    }
)
REJECTED_CODES = frozenset(
    {
        "DataInspectionFailed",
        "content_filter",
        "incorrect_api_key",
        "invalid_parameter_error",
        "InvalidParameter",
        "invalid_prompt",
        "invalid_request_error",
        "model_not_found",
    }
)
SAFE_CODES = REJECTED_CODES | {
    "batch_expired",
    "batch_cancelled",
    "InternalError",
    "RateLimitExceeded",
    "rate_limit_error",
    "request_timeout",
    "server_error",
}


class OversizedBatchLine(Exception):
    """The stage can still be claimed by the real-time worker."""


@dataclass(frozen=True)
class RequestLine:
    custom_id: str
    data: bytes
    frame_count: int | None


def build_line(
    custom_id: str,
    profile: AiProfile,
    kind: TaskKind,
    material: VideoMaterial | list[Recognition],
    max_bytes: int,
) -> RequestLine:
    limit = min(LINE_LIMIT, max_bytes)
    frame_count = None
    if isinstance(material, VideoMaterial):
        frame_count = len(material.frames)
        if sum(4 * ((frame.stat().st_size + 2) // 3) for frame in material.frames) > limit:
            raise OversizedBatchLine
        content = [{"type": "text", "text": recognition_prompt(material, frame_count)}]
        for frame in material.frames:
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
            )
    else:
        content = summary_prompt(material)
    tool = convert_to_openai_tool(SCHEMAS[kind])
    body = {
        **_parameters(profile),
        "model": profile.model,
        "messages": [
            {"role": "system", "content": profile.prompt},
            {"role": "user", "content": content},
        ],
        "tools": [tool],
        "tool_choice": {"type": "function", "function": {"name": tool["function"]["name"]}},
    }
    row = {"custom_id": custom_id, "method": "POST", "url": CHAT_COMPLETIONS, "body": body}
    data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(data) > limit:
        raise OversizedBatchLine
    return RequestLine(custom_id, data, frame_count)


def _parameters(profile: AiProfile) -> dict:
    extra = profile.params.get("extra_body") or {}
    params = {key: extra[key] for key in PARAMETERS if key in extra}
    params.update({key: profile.params[key] for key in PARAMETERS if key in profile.params})
    params.setdefault("enable_thinking", False)
    if not params["enable_thinking"]:
        params.pop("thinking_budget", None)
    return params


def safe_code(value: object) -> str | None:
    return value if isinstance(value, str) and value in SAFE_CODES else None


def provider_error(code: object = None, status: int | None = None) -> Exception:
    label = safe_code(code)
    details = " ".join(str(part) for part in (status, label) if part is not None)
    message = "Bailian Batch request failed"
    if details:
        message += f" ({details})"
    if label in REJECTED_CODES or (
        status is not None and 400 <= status < 500 and status not in RETRYABLE_STATUS
    ):
        return Rejected(message)
    return RuntimeError(message)


def parse_result(kind: TaskKind, row: dict) -> Answer[Recognition | CreatorTags] | Exception:
    response = row.get("response")
    response = response if isinstance(response, dict) else {}
    body = response.get("body")
    body = body if isinstance(body, dict) else {}
    status = response.get("status_code")
    error = row.get("error") or body.get("error")
    if error or status != 200:
        code = error.get("code") if isinstance(error, dict) else body.get("code")
        return provider_error(code, status if isinstance(status, int) else None)
    try:
        message = body["choices"][0]["message"]
        schema = SCHEMAS[kind]
        calls = message.get("tool_calls") or []
        if calls:
            arguments = next(
                call["function"]["arguments"]
                for call in calls
                if call.get("function", {}).get("name") == schema.__name__
            )
        else:
            arguments = message["content"]
        value = schema.model_validate_json(arguments)
    except (AttributeError, IndexError, KeyError, StopIteration, TypeError, ValidationError):
        return RuntimeError("Bailian Batch returned an invalid structured result")
    return Answer(value, _usage(body.get("usage")))


def _usage(data: dict | None) -> Usage | None:
    if not isinstance(data, dict):
        return None
    source, output = data.get("prompt_tokens"), data.get("completion_tokens")
    if not isinstance(source, int) or not isinstance(output, int):
        return None
    details = data.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return Usage(source, output, cached if isinstance(cached, int) else None)
