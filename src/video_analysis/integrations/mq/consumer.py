"""接收调度中心投递的任务：消息参数是一个 JSON 对象。

格式错误的消息直接进入死信队列；登记失败的消息按 Dramatiq 的退避策略重试。
"""

import dramatiq
from dramatiq import Broker
from pydantic import ValidationError

from video_analysis.core.database.engine import Database
from video_analysis.modules.analysis.upstream.messages import AnalyzeVideo, SummarizeCreator
from video_analysis.modules.analysis.upstream.service import UpstreamService


def declare_actors(broker: Broker, database: Database, queue: str) -> None:
    async def analyze_video(payload: dict) -> None:
        message = AnalyzeVideo.model_validate(payload)
        async with database.transaction() as session:
            await UpstreamService(session).accept_video(message)

    async def summarize_creator(payload: dict) -> None:
        message = SummarizeCreator.model_validate(payload)
        async with database.transaction() as session:
            await UpstreamService(session).accept_summary(message)

    for handler in (analyze_video, summarize_creator):
        dramatiq.actor(broker=broker, queue_name=queue, throws=(ValidationError,))(handler)
