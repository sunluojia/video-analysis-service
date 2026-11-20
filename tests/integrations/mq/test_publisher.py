from dramatiq import Message
from dramatiq.brokers.stub import StubBroker
from sqlalchemy import func, select

from tests.factories import accept, video_message
from video_analysis.core.messaging.outbox import outbox
from video_analysis.integrations.mq.publisher import EventPublisher


async def unpublished(database) -> int:
    async with database.engine.connect() as connection:
        stmt = select(func.count()).where(outbox.c.published_at.is_(None))
        return (await connection.execute(stmt)).scalar_one()


async def test_events_are_sent_to_the_scheduler_queue_once(database):
    broker = StubBroker()
    publisher = EventPublisher(database, broker, "analysis.events", "analysis_event")
    await accept(database, video_message())

    await publisher.publish_pending()
    await publisher.publish_pending()

    assert await unpublished(database) == 0
    [encoded] = list(broker.queues["analysis.events"].queue)
    message = Message.decode(encoded)
    [event] = message.args
    assert (message.actor_name, message.message_id) == ("analysis_event", event["event_id"])
    assert (event["job_id"], event["status"]) == ("j-1", "accepted")
