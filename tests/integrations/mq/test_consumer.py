import asyncio

import dramatiq
import pytest
from dramatiq import Message
from dramatiq.asyncio import get_event_loop_thread
from dramatiq.brokers.stub import StubBroker
from dramatiq.middleware import AsyncIO

from tests.factories import events, video_message
from video_analysis.core.database.engine import Database
from video_analysis.integrations.mq.consumer import declare_actors

QUEUE = "analysis.tasks"


@pytest.fixture
async def broker(database_url, database):
    """处理器运行在 Dramatiq 自己的事件循环里，使用单独的连接池。"""
    broker = StubBroker()
    broker.add_middleware(AsyncIO())
    actors_database = Database(database_url)
    declare_actors(broker, actors_database, QUEUE)
    worker = dramatiq.Worker(broker, worker_timeout=100)
    worker.start()
    yield broker
    loop_thread = get_event_loop_thread()
    assert loop_thread is not None
    loop_thread.run_coroutine(actors_database.dispose())
    worker.stop()


def deliver(broker: StubBroker, actor: str, payload: dict) -> None:
    broker.enqueue(Message(QUEUE, actor, (payload,), {}, {}))
    broker.join(QUEUE, timeout=10_000, fail_fast=False)


async def test_valid_message_is_accepted(broker, database):
    await asyncio.to_thread(
        deliver, broker, "analyze_video", video_message().model_dump(mode="json")
    )

    [event] = await events(database)
    assert (event["job_id"], event["status"]) == ("j-1", "accepted")
    assert broker.dead_letters == []


async def test_malformed_message_goes_straight_to_dead_letters(broker, database):
    await asyncio.to_thread(deliver, broker, "analyze_video", {"job_id": "j-1"})

    # 不经退避重试，处理一次就进入死信。
    assert await events(database) == []
    [dead] = broker.dead_letters
    assert dead.actor_name == "analyze_video"
