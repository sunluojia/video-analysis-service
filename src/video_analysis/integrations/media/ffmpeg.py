"""FFmpeg／FFprobe 子进程。超时由调度层控制，取消时结束子进程。"""

import asyncio
import json
import re
import shutil
from pathlib import Path

from video_analysis.modules.analysis.job.models import MediaInfo

MAX_VOLUME = re.compile(r"max_volume: (\S+) dB")
DIGITAL_SILENCE_DB = -91.0
"""volumedetect 对全零采样报告的下限；最小的非零采样约为 -90.3 dB。"""


def missing_tools() -> list[str]:
    """PATH 中找不到的 FFmpeg 命令。"""
    return [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]


def not_found(*tools: str) -> str:
    return f"未找到 {'、'.join(tools)}，请在分析服务所在机器安装 FFmpeg 并加入 PATH"


class FfmpegMedia:
    def __init__(self, *, frame_count: int, segment_seconds: int) -> None:
        self.frame_count = frame_count
        self.segment_seconds = segment_seconds

    @staticmethod
    async def probe(source: Path) -> MediaInfo:
        output, _ = await _run(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(source),
        )  # fmt: skip
        info = json.loads(output)
        streams = {stream["codec_type"] for stream in info.get("streams", [])}
        duration = float(info.get("format", {}).get("duration", 0))
        if "video" not in streams or duration <= 0:
            raise ValueError("不是有效的视频文件")
        return MediaInfo(duration=duration, has_audio="audio" in streams)

    async def extract_frames(self, source: Path, target: Path) -> int:
        """在整段视频上均匀抽取图片，第 k 张约在 k × 时长 ÷ 张数 处。"""
        info = await self.probe(source)
        await _run(
            "ffmpeg", "-v", "error", "-i", str(source),
            "-vf", f"fps={self.frame_count / info.duration},scale=-2:'min(720,ih)'",
            "-frames:v", str(self.frame_count), "-start_number", "0", str(target / "%03d.jpg"),
        )  # fmt: skip
        return len(list(target.glob("*.jpg")))

    async def extract_audio(self, source: Path, target: Path) -> int:
        """切成固定时长的单声道小文件。只有确定是数字静音才跳过，低音量的讲话和音乐照常听音。

        没测出音量时照常切段；音频无法解码时自然得到 0 段。
        """
        _, stats = await _run(
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(source),
            "-vn", "-af", "volumedetect", "-f", "null", "-",
        )  # fmt: skip
        volume = MAX_VOLUME.search(stats)
        if volume and float(volume[1]) <= DIGITAL_SILENCE_DB:
            return 0
        await _run(
            "ffmpeg", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "48k", "-f", "segment", "-segment_time", str(self.segment_seconds),
            str(target / "%03d.mp3"),
        )  # fmt: skip
        return len(list(target.glob("*.mp3")))


async def _run(*args: str) -> tuple[str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise RuntimeError(not_found(args[0])) from None
    try:
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    output, errors = stdout.decode(), stderr.decode(errors="replace")
    if process.returncode:
        raise RuntimeError(f"{args[0]} 退出码 {process.returncode}：{errors.strip()[-500:]}")
    return output, errors
