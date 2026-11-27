from sqlalchemy import Boolean, Column, String, Table, false

from video_analysis.core.database.mapping import metadata, timestamp

batches = Table(
    "batches",
    metadata,
    # 调度中心的批次标识。
    Column("id", String(128), primary_key=True),
    Column("paused", Boolean, nullable=False),
    # 取消后到达的任务不再受理。
    Column("cancelled", Boolean, nullable=False, server_default=false()),
    Column("last_dispatched_at", timestamp()),
    Column("ai_batch_mode", Boolean, nullable=False, server_default=false()),
)
