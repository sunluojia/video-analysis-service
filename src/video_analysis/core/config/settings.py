import socket
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field, PrivateAttr, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程运行配置，读取 VA_ 前缀的环境变量或 .env；每个进程只读取自己需要的部分。

    AI 供应商、模型、额度和提示词通过接口配置，保存在数据库中。
    """

    model_config = SettingsConfigDict(env_prefix="VA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://video:video@localhost:5432/video_analysis"
    redis_url: str = "redis://localhost:6379/0"
    queue_namespace: str = "dramatiq"
    """与调度中心使用同一个 Redis 和命名空间。"""
    task_queue: str = "analysis.tasks"
    event_queue: str = "analysis.events"
    event_actor: str = "analysis_event"
    """调度中心接收事件的处理器名。"""
    log_level: str = "INFO"


class ServiceSettings(Settings):
    """与调度中心互相调用、使用工作目录的进程：API 和 Worker。"""

    scheduler_url: str = "http://localhost:8080"
    service_token: SecretStr = Field(min_length=16)
    """两个服务之间互相调用的服务凭据。"""
    work_root: Path = Path("data/work")
    """下载的视频、抽帧图片和音频段，Worker 与 API 共享。"""
    listen_per_video: int = 2


class WorkerSettings(ServiceSettings):
    model_config = SettingsConfigDict(validate_by_name=True)

    worker_id: str = Field(
        default_factory=socket.gethostname,
        validation_alias="VIDEO_ANALYSIS_WORKER_ID",
        min_length=1,
        max_length=128,
    )
    _worker_session_id: UUID = PrivateAttr(default_factory=uuid4)

    artifact_retention_hours: float = 72
    artifact_cleanup_enabled: bool = True
    """任务结束后，抽帧图片和音频段保留的时长。"""
    download_slots: int = 4
    media_slots: int = 4
    admission_factor: float = 2
    """等待模型的工作低于模型并发额度之和的这个倍数时，开始新视频。"""
    media_backlog: int = 350
    """处理中视频数的上限，只用来保护磁盘。"""
    min_free_disk_gb: float = 10
    media_timeout_seconds: float = 300
    frame_count: int = 6
    audio_segment_seconds: int = 60
    thread_pool_size: int = 4
    poll_interval_seconds: float = 1
    ai_batch_max_items: int = Field(default=300, ge=1, le=10_000)
    ai_batch_flush_seconds: float = Field(default=120, gt=0)
    ai_batch_max_bytes: int = Field(default=80_000_000, ge=1, le=500_000_000)
    ai_batch_poll_seconds: float = Field(default=25, gt=0)
    ai_batch_max_active: int = Field(default=20, ge=1)
    max_attempts: int = 3
    retry_delay_seconds: float = 30

    @property
    def worker_session_id(self) -> UUID:
        return self._worker_session_id


class ApiSettings(ServiceSettings):
    api_host: str = "127.0.0.1"
    """默认只监听本机；部署时改为 0.0.0.0，供调度中心经局域网访问。"""
    api_port: int = 8000
    instance_id: str = Field(default_factory=socket.gethostname)
    public_url: str = "http://localhost:8000"
    """调度中心访问本服务的地址，注册时上报。"""
    heartbeat_seconds: float = 15
