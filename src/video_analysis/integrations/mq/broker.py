from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AsyncIO

from video_analysis.core.config.settings import Settings


def create_broker(settings: Settings) -> RedisBroker:
    """与调度中心共用的 Redis 和命名空间；处理器是协程，需要 AsyncIO 中间件提供事件循环。"""
    broker = RedisBroker(url=settings.redis_url, namespace=settings.queue_namespace)
    broker.add_middleware(AsyncIO())
    return broker
