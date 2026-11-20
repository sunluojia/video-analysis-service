"""任务接收进程，由 Dramatiq 命令行启动，可以开多个进程：

dramatiq video_analysis.entrypoints.consumer:broker
"""

from video_analysis.core.config.settings import Settings
from video_analysis.core.database.engine import Database
from video_analysis.integrations.mq.broker import create_broker
from video_analysis.integrations.mq.consumer import declare_actors

settings = Settings()
broker = create_broker(settings)
declare_actors(broker, Database(settings.database_url), settings.task_queue)
