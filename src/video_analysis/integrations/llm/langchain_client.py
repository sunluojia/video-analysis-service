"""用 LangChain 统一调用听音、视频识别和作者汇总。

超时、重试和并发额度由调度层负责，这里组织输入、解析结构化输出，并标出重发也没用的请求。
"""

import asyncio
import base64
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ContentBlock, HumanMessage, SystemMessage
from langchain_core.messages.content import (
    create_audio_block,
    create_image_block,
    create_text_block,
)
from openai import APIStatusError
from pydantic import BaseModel

from video_analysis.modules.ai.call.formatting import (
    filter_appearances,
    recognition_prompt,
    summary_prompt,
)
from video_analysis.modules.ai.call.models import (
    BAILIAN_EXPLICIT_CACHE,
    EXPLICIT_CACHE_MODELS,
    AiProfile,
    Usage,
)
from video_analysis.modules.analysis.job.results import CreatorTags, Listening, Recognition
from video_analysis.modules.scheduling.execution.ports import (
    Answer,
    InvalidAudio,
    Rejected,
    VideoMaterial,
)

RETRYABLE_STATUS = frozenset({408, 409, 429})
"""与 OpenAI SDK 的重试判断一致：请求超时、冲突、限流和 5xx 重发可能成功，其余 4xx 重发结果相同。"""
_INVALID_AUDIO_MESSAGES = ("the audio is empty", "the audio format is illegal")
log = logging.getLogger(__name__)


class LangChainAi:
    def __init__(self) -> None:
        self._models: dict[tuple, BaseChatModel] = {}

    async def listen(self, profile: AiProfile, audio: Path) -> Answer[Listening]:
        encoded = _base64(await asyncio.to_thread(audio.read_bytes))
        audio_block: ContentBlock | dict[str, Any]
        if _is_bailian_openai(profile):
            # 百炼的 Chat Completions 要求本地音频带完整的 Data URI。
            audio_block = {
                "type": "input_audio",
                "input_audio": {"data": f"data:audio/mp3;base64,{encoded}", "format": "mp3"},
            }
        else:
            audio_block = create_audio_block(base64=encoded, mime_type="audio/mpeg")
        return await self._ask(
            profile, Listening, create_text_block("请分析这段音频。"), audio_block
        )

    async def recognize(self, profile: AiProfile, material: VideoMaterial) -> Answer[Recognition]:
        frames = await asyncio.to_thread(lambda: [path.read_bytes() for path in material.frames])
        text = recognition_prompt(material, len(frames))
        images = [
            create_image_block(base64=_base64(data), mime_type="image/jpeg") for data in frames
        ]
        answer = await self._ask(profile, Recognition, create_text_block(text), *images)
        filter_appearances(answer.value, len(frames))
        return answer

    async def summarize(
        self, profile: AiProfile, videos: Sequence[Recognition]
    ) -> Answer[CreatorTags]:
        answer = await self._ask(profile, CreatorTags, create_text_block(summary_prompt(videos)))
        return replace(answer, value=answer.value.with_observed_tags(videos))

    async def _ask[T: BaseModel](
        self, profile: AiProfile, schema: type[T], *blocks: ContentBlock | dict[str, Any]
    ) -> Answer[T]:
        # Pydantic 定义字段结构，系统提示词定义业务判断；所选模型需支持工具调用。
        model = self._model(profile).with_structured_output(
            schema, method="function_calling", include_raw=True
        )
        explicit_cache = (
            _is_bailian_openai(profile)
            and profile.model in EXPLICIT_CACHE_MODELS
            and profile.params.get(BAILIAN_EXPLICIT_CACHE) is not False
            and not profile.params.get("use_responses_api")
        )
        system: str | list[str | dict[str, Any]] = profile.prompt
        if explicit_cache:
            # 工具定义随系统提示词参与缓存，各任务的素材都在之后的用户消息中。
            system = [
                {
                    "type": "text",
                    "text": profile.prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        messages = [SystemMessage(system), HumanMessage(content=[dict(b) for b in blocks])]
        try:
            answer = await model.ainvoke(messages)
        except APIStatusError as error:
            if error.status_code < 500 and error.status_code not in RETRYABLE_STATUS:
                message = str(error)
                if schema is Listening and error.status_code == 400:
                    # SDK 的异常消息可能省略独立的审核错误码。
                    reason = f"{error.code or ''} {error.type or ''} {message}".casefold()
                    if (
                        "datainspectionfailed" not in reason
                        and "inappropriate content" not in reason
                        and any(text in reason for text in _INVALID_AUDIO_MESSAGES)
                    ):
                        raise InvalidAudio(message) from error
                raise Rejected(message) from error
            raise
        raw, usage = answer["raw"], None
        if isinstance(raw, AIMessage) and (metadata := raw.usage_metadata):
            usage = Usage(
                input_tokens=metadata["input_tokens"],
                output_tokens=metadata["output_tokens"],
                cached_tokens=metadata.get("input_token_details", {}).get("cache_read"),
            )
            log.info(
                "prompt_cache_usage model=%s schema=%s prompt_version=%s "
                "mode=%s prompt_tokens=%d cached_tokens=%s",
                profile.label,
                schema.__name__,
                profile.prompt_version,
                "explicit" if explicit_cache else "default",
                usage.input_tokens,
                usage.cached_tokens,
            )
        if error := answer["parsing_error"]:
            raise error
        return Answer(schema.model_validate(answer["parsed"]), usage)

    def _model(self, profile: AiProfile) -> BaseChatModel:
        options: dict[str, Any] = profile.model_params
        key = (
            profile.provider,
            profile.model,
            profile.base_url,
            profile.credential_ref,
            json.dumps(options, sort_keys=True),
        )
        if key not in self._models:
            options["max_retries"] = 0
            if _is_bailian_openai(profile):
                options.setdefault("stream_usage", True)
            if profile.base_url:
                options["base_url"] = profile.base_url
            if profile.credential_ref:
                options["api_key"] = _secret(profile.credential_ref)
            self._models[key] = init_chat_model(
                profile.model, model_provider=profile.provider, **options
            )
        return self._models[key]


def _is_bailian_openai(profile: AiProfile) -> bool:
    if profile.provider != "openai" or not profile.base_url:
        return False
    host = urlsplit(profile.base_url).hostname or ""
    return host.endswith((".maas.aliyuncs.com", ".dashscope.aliyuncs.com")) or host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    }


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise LookupError(f"环境变量 {name} 未设置")
    return value


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode()
