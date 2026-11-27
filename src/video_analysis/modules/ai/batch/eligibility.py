from urllib.parse import urlsplit

from video_analysis.modules.scheduling.task.models import TaskKind

BATCH_KINDS = frozenset({TaskKind.RECOGNIZE, TaskKind.SUMMARIZE})
"""B-machine probes support qwen3.8-flash; qwen3.8-omni-flash returns model_not_found."""


def eligible(kind: TaskKind, provider: str, model: str, base_url: str | None) -> bool:
    # The Batch deployment for this model is currently in DashScope's Beijing region.
    if kind not in BATCH_KINDS or provider != "openai" or model != "qwen3.8-flash" or not base_url:
        return False
    try:
        endpoint = urlsplit(base_url)
    except ValueError:
        return False
    return endpoint.scheme == "https" and endpoint.hostname == "dashscope.aliyuncs.com"
