import json
from datetime import datetime

from openai import APIError, APIStatusError, AsyncOpenAI
from openai.types import Batch

from video_analysis.integrations.llm.batch_payload import CHAT_COMPLETIONS, provider_error
from video_analysis.integrations.llm.langchain_client import _secret
from video_analysis.modules.ai.call.models import AiProfile


class BatchClient:
    def __init__(self, profile: AiProfile) -> None:
        self._client = AsyncOpenAI(
            api_key=_secret(profile.credential_ref) if profile.credential_ref else None,
            base_url=profile.base_url,
            timeout=60,
            # A timed-out POST may have created a remote job. Reconcile before retrying.
            max_retries=0,
        )

    async def __aenter__(self) -> "BatchClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._client.close()

    async def upload(self, filename: str, payload: bytes) -> str:
        file = await self._client.files.create(
            file=(filename, payload, "application/jsonl"), purpose="batch"
        )
        return file.id

    async def submit(self, file_id: str) -> Batch:
        return await self._client.batches.create(
            input_file_id=file_id, endpoint=CHAT_COMPLETIONS, completion_window="24h"
        )

    async def retrieve(self, batch_id: str) -> Batch:
        return await self._client.batches.retrieve(batch_id)

    async def find(self, file_id: str, since: datetime) -> Batch | None:
        """Look up a possibly accepted POST by its unique uploaded file."""
        async for batch in self._client.batches.list(limit=100):
            if batch.input_file_id == file_id:
                return batch
            if batch.created_at is not None and batch.created_at < since.timestamp() - 60:
                break
        return None

    async def results(self, file_id: str) -> dict[str, dict]:
        response = await self._client.files.content(file_id)
        results = {}
        for line in response.content.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("custom_id"), str):
                results.setdefault(row["custom_id"], row)
        return results


def describe_batch_error(error: Exception, operation: str) -> Exception:
    """Keep provider bodies, URLs and request headers out of logs and persisted errors."""
    if isinstance(error, APIStatusError):
        return provider_error(error.code, error.status_code)
    reason = "network error" if isinstance(error, APIError) else "client configuration error"
    return RuntimeError(f"Bailian Batch {operation} failed ({reason})")
