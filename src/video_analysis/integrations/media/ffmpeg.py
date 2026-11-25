"""FFmpeg／FFprobe 子进程。超时由调度层控制，取消时结束子进程。"""

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from video_analysis.modules.analysis.job.models import MediaInfo

log = logging.getLogger(__name__)
MAX_VOLUME = re.compile(r"max_volume: (\S+) dB")
DIGITAL_SILENCE_DB = -91.0
"""volumedetect 对全零采样报告的下限；最小的非零采样约为 -90.3 dB。"""


_FRAME_RETRY_ERROR = re.compile(
    r"invalid (?:color space|argument)|could not open encoder|error (?:while )?opening encoder",
    re.IGNORECASE,
)
_FAILED_VIDEO_STREAM = re.compile(
    r"input from stream 0:(\d+)\b.*invalid color space", re.IGNORECASE
)
_COLOR_METADATA = {
    "color_space": "matrix_coefficients",
    "color_primaries": "colour_primaries",
    "color_transfer": "transfer_characteristics",
}
_RESERVED_COLOR_VALUES = {"reserved", "reserved0"}


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
        output_args = (
            "-vf", f"fps={self.frame_count / info.duration},scale=-2:'min(720,ih)'",
            "-frames:v", str(self.frame_count), "-start_number", "0",
        )  # fmt: skip
        try:
            await _run(
                "ffmpeg", "-v", "error", "-i", str(source), *output_args, str(target / "%03d.jpg")
            )
        except _ProcessError as error:
            # EINVAL (-22) 在 POSIX 进程退出码中截断为 234。
            if error.returncode not in (-22, 234) and not _FRAME_RETRY_ERROR.search(error.stderr):
                raise
            try:
                count = await self._retry_frames(source, target, output_args, error.stderr)
            except _ProcessError as retry_error:
                raise RuntimeError(f"{error}\n色彩元数据回退失败：{retry_error}") from retry_error
            if count is None:
                raise
            return count
        return len(list(target.glob("*.jpg")))

    async def _retry_frames(
        self, source: Path, target: Path, output_args: tuple[str, ...], stderr: str
    ) -> int | None:
        if (patch := await _color_patch(source, stderr)) is None:
            return None
        index, bitstream_filter = patch

        # 滤镜图在首帧进入前就会拒绝非法矩阵，因此先在压缩流上改写标签。
        with TemporaryDirectory(dir=target, prefix=".normalize-") as directory:
            scratch = Path(directory)
            normalized = scratch / "source.mp4"
            await _run(
                "ffmpeg", "-v", "error", "-i", str(source), "-map", f"0:{index}",
                "-c:v", "copy", "-bsf:v", bitstream_filter, str(normalized),
            )  # fmt: skip
            output = str(scratch / "%03d.jpg")
            await _run("ffmpeg", "-v", "error", "-i", str(normalized), *output_args, output)
            frames = list(scratch.glob("*.jpg"))
            if not frames:
                return None
            for frame in frames:
                frame.replace(target / frame.name)
            # target 由调度层清空。首次失败可能留下比回退输出更多的帧。
            for stale in range(len(frames), self.frame_count):
                (target / f"{stale:03d}.jpg").unlink(missing_ok=True)
        log.info("抽帧色彩元数据回退成功（%s）", source.parent.name)
        return len(frames)

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


async def _color_patch(source: Path, stderr: str) -> tuple[int, str] | None:
    output, _ = await _run(
        "ffprobe", "-v", "error", "-select_streams", "V", "-show_entries",
        "stream=index,codec_name,color_space,color_primaries,color_transfer", "-of", "json",
        str(source),
    )  # fmt: skip
    streams = json.loads(output).get("streams", [])
    if match := _FAILED_VIDEO_STREAM.search(stderr):
        index = int(match[1])
        stream = next((stream for stream in streams if stream["index"] == index), None)
    else:
        # stderr 未标出输入流时，仅在能确定视频流的情况下回退。
        stream = streams[0] if len(streams) == 1 else None
    if stream is None or stream.get("codec_name") not in {"h264", "hevc"}:
        return None

    # H.264/HEVC 中 2 表示 unspecified。只改写保留值，保留有效的色彩标签和范围。
    options = [
        f"{option}=2"
        for tag, option in _COLOR_METADATA.items()
        if stream.get(tag) in _RESERVED_COLOR_VALUES
    ]
    if not options:
        return None
    return stream["index"], f"{stream['codec_name']}_metadata={':'.join(options)}"


class _ProcessError(RuntimeError):
    def __init__(self, tool: str, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{tool} 退出码 {returncode}：{stderr.strip()[-500:]}")


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
        raise _ProcessError(args[0], process.returncode, errors)
    return output, errors
