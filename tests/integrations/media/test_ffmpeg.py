import shutil
import subprocess
from pathlib import Path

import pytest

from video_analysis.integrations.media.ffmpeg import FfmpegMedia, missing_tools

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要安装 FFmpeg")


def make_video(path: Path, audio: str) -> Path:
    """5 秒测试画面，audio 为 lavfi 音频滤镜图。"""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
         "-f", "lavfi", "-i", audio, "-shortest", "-f", "mp4", str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture
def media() -> FfmpegMedia:
    return FfmpegMedia(frame_count=4, segment_seconds=2)


def empty_dir(path: Path) -> Path:
    path.mkdir()
    return path


@needs_ffmpeg
async def test_prepares_frames_and_audio_segments(media, tmp_path):
    source = make_video(tmp_path / "source", "sine=frequency=440:duration=5")

    info = await media.probe(source)
    assert info.has_audio
    assert info.duration == pytest.approx(5, abs=0.5)

    frames = empty_dir(tmp_path / "frames")
    assert await media.extract_frames(source, frames) == 4
    assert (frames / "000.jpg").exists()

    audio = empty_dir(tmp_path / "audio")
    segments = await media.extract_audio(source, audio)
    assert segments >= 2
    assert (audio / f"{segments - 1:03d}.mp3").exists()


@needs_ffmpeg
async def test_digital_silence_needs_no_listening(media, tmp_path):
    source = make_video(tmp_path / "source", "anullsrc=r=16000:cl=mono:duration=5")

    assert await media.extract_audio(source, empty_dir(tmp_path / "audio")) == 0


@needs_ffmpeg
async def test_quiet_audio_is_still_listened_to(media, tmp_path):
    # 峰值约 -68 dB：低于旧的 -50 dB 阈值，但不是数字静音。
    source = make_video(tmp_path / "source", "sine=frequency=440:duration=5,volume=-50dB")

    assert await media.extract_audio(source, empty_dir(tmp_path / "audio")) >= 2


@needs_ffmpeg
async def test_rejects_files_without_video(media, tmp_path):
    broken = tmp_path / "source"
    broken.write_bytes(b"not a video")

    with pytest.raises(RuntimeError):
        await media.probe(broken)


async def test_missing_ffmpeg_says_what_to_install(media, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    assert missing_tools() == ["ffmpeg", "ffprobe"]
    with pytest.raises(RuntimeError) as raised:
        await media.probe(tmp_path / "source")
    assert str(raised.value) == "未找到 ffprobe，请在分析服务所在机器安装 FFmpeg 并加入 PATH"
