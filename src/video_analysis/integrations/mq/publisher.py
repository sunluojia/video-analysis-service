import asyncio
import logging

from dramatiq import Broker, Message

from video_analysis.core.common.clock import utcnow
from video_analysis.core.database.engine import Database
from video_analysis.core.messaging.outbox import Outbox

log = logging.getLogger(__name__)


class EventPublisher:
    """把发件箱中的事件投递到调度中心的事件队列。

    写入 Redis 成功后才标记已发布，至少投递一次；消息 ID 即事件 ID，调度中心据此去重。
    """

    def __init__(self, database: Database, broker: Broker, queue: str, actor: str) -> None:
        broker.declare_queue(queue)
        self._database = database
        self._broker = broker
        self._queue = queue
        self._actor = actor

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.publish_pending()
            except Exception as error:
                log.error("投递事件失败，事件保留在发件箱，稍后重试", exc_info=error)
            await asyncio.sleep(interval)

    async def publish_pending(self) -> None:
        async with self._database.transaction() as session:
            outbox = Outbox(session)
            pending = await outbox.pending(limit=100)
            if pending:
                await asyncio.to_thread(self._send, [payload for _, payload in pending])
                await outbox.mark_published([row_id for row_id, _ in pending], utcnow())

    def _send(self, payloads: list[dict]) -> None:
        for payload in payloads:
            message = Message(
                queue_name=self._queue,
                actor_name=self._actor,
                args=(payload,),
                kwargs={},
                options={},
                message_id=payload["event_id"],
            )
            self._broker.enqueue(message)
