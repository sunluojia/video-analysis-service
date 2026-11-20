import asyncio
import hashlib
import logging
from pathlib import Path
from urllib.parse import quote

import anyio
import httpx

from video_analysis.modules.analysis.job.models import Material

log = logging.getLogger(__name__)


class SchedulerClient:
    """以服务凭据调用调度中心：下载原视频，注册本服务并保持心跳。"""

    def __init__(
        self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def download(self, material: Material, target: Path) -> None:
        """断点续传到同目录的 .part 文件，大小和 SHA-256 一致后改名。

        不一致时删除，重试时从头下载。
        """
        partial = target.with_name(f"{target.name}.part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset < material.size:
            await self._fetch(material, partial, offset)
        if await asyncio.to_thread(_digest, partial) != (material.size, material.sha256):
            partial.unlink()
            raise ValueError("下载的视频与登记的大小或 SHA-256 不一致")
        partial.replace(target)

    async def _fetch(self, material: Material, partial: Path, offset: int) -> None:
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        url = f"/api/materials/{quote(material.id, safe='')}"
        async with self._http.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            # 调度中心没有按范围返回时整份重写。
            mode = "ab" if response.status_code == httpx.codes.PARTIAL_CONTENT else "wb"
            async with await anyio.open_file(partial, mode) as file:
                async for chunk in response.aiter_bytes():
                    await file.write(chunk)

    async def keep_registered(
        self, instance_id: str, url: str, version: str, interval: float
    ) -> None:
        """定期上报访问地址和版本，注册和心跳是同一个请求；调度中心可以晚于本服务启动。"""
        while True:
            try:
                body = {"url": url, "version": version}
                (await self._http.put(_service_path(instance_id), json=body)).raise_for_status()
            except httpx.HTTPError as error:
                log.warning("向调度中心注册失败，稍后重试：%s", error)
            await asyncio.sleep(interval)

    async def withdraw(self, instance_id: str) -> None:
        """停止时注销，调度中心不必等心跳超时。"""
        (await self._http.delete(_service_path(instance_id))).raise_for_status()


def _service_path(instance_id: str) -> str:
    return f"/api/analysis-services/{quote(instance_id, safe='')}"


def _digest(path: Path) -> tuple[int, str]:
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256")
    return path.stat().st_size, digest.hexdigest()
