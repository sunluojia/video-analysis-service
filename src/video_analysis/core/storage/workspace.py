"""工作目录：每个任务一个子目录，存放下载的视频、抽帧图片和音频段，多个 Worker 共享。"""

import asyncio
import shutil
from pathlib import Path
from uuid import UUID


class Workspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    def source(self, key: UUID) -> Path:
        return self._root / str(key) / "source"

    def frames(self, key: UUID) -> list[Path]:
        return sorted((self._root / str(key) / "frames").glob("*.jpg"))

    def frame(self, key: UUID, index: int) -> Path:
        """图片按 000.jpg、001.jpg 的顺序命名。"""
        return self._root / str(key) / "frames" / f"{index:03d}.jpg"

    def segment(self, key: UUID, index: int) -> Path:
        """音频段按 000.mp3、001.mp3 的顺序命名。"""
        return self._root / str(key) / "audio" / f"{index:03d}.mp3"

    async def fresh(self, key: UUID, name: str) -> Path:
        """准备一个空目录；重跑时先清掉上次留下的文件。"""
        path = self._root / str(key) / name
        await asyncio.to_thread(_remove, path)
        path.mkdir(parents=True)
        return path

    async def discard_source(self, key: UUID) -> None:
        await asyncio.to_thread(self.source(key).unlink, missing_ok=True)

    async def discard(self, key: UUID) -> None:
        await asyncio.to_thread(_remove, self._root / str(key))

    def free_bytes(self) -> int:
        self._root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self._root).free


def _remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
