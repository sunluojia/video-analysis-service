import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from video_analysis.integrations.media import ffmpeg
from video_analysis.integrations.media.ffmpeg import FfmpegMedia, missing_tools

needs_ffmpeg = pytest.mark.skipif(bool(missing_tools()), reason="需要安装 FFmpeg 和 FFprobe")
VIDEO_PROBE = json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": 4}})


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
async def test_prepares_frames_and_audio_segments(media, tmp_path, monkeypatch):
    source = make_video(tmp_path / "source", "sine=frequency=440:duration=5")

    info = await media.probe(source)
    assert info.has_audio
    assert info.duration == pytest.approx(5, abs=0.5)

    run = AsyncMock(wraps=ffmpeg._run)
    monkeypatch.setattr(ffmpeg, "_run", run)
    frames = empty_dir(tmp_path / "frames")
    assert await media.extract_frames(source, frames) == 4
    assert (frames / "000.jpg").exists()
    assert [call.args[0] for call in run.await_args_list] == ["ffprobe", "ffmpeg"]

    audio = empty_dir(tmp_path / "audio")
    segments = await media.extract_audio(source, audio)
    assert segments >= 2
    assert (audio / f"{segments - 1:03d}.mp3").exists()


@pytest.mark.parametrize(
    ("returncode", "stderr", "streams"),
    [
        (1, "No space left on device", None),
        (234, "Invalid argument", [{"index": 1, "codec_name": "h264", "color_space": "bt709"}]),
        (
            234,
            "Invalid color space",
            [{"index": 1, "codec_name": "vp9", "color_space": "reserved"}],
        ),
        (
            1,
            "Could not open encoder before EOF",
            [
                {"index": 1, "codec_name": "h264", "color_space": "bt709"},
                {"index": 2, "codec_name": "h264", "color_space": "reserved"},
            ],
        ),
    ],
)
async def test_frame_retry_requires_reserved_metadata_on_a_known_stream(
    media, tmp_path, monkeypatch, returncode, stderr, streams
):
    error = ffmpeg._ProcessError("ffmpeg", returncode, stderr)
    responses = [(VIDEO_PROBE, ""), error]
    if streams is not None:
        responses.append((json.dumps({"streams": streams}), ""))
    run = AsyncMock(side_effect=responses)
    monkeypatch.setattr(ffmpeg, "_run", run)

    with pytest.raises(RuntimeError) as raised:
        await media.extract_frames(tmp_path / "source", empty_dir(tmp_path / "frames"))

    assert raised.value is error
    assert run.await_count == len(responses)


async def test_retries_the_failed_hevc_stream_and_replaces_partial_frames(
    media, tmp_path, monkeypatch
):
    source = tmp_path / "source"
    frames = empty_dir(tmp_path / "frames")
    commands = []
    streams = [
        {"index": 1, "codec_name": "h264", "color_space": "reserved"},
        {
            "index": 2,
            "codec_name": "hevc",
            "color_space": "reserved",
            "color_primaries": "bt2020",
            "color_transfer": "reserved0",
        },
    ]

    async def run(*args):
        commands.append(args)
        if args[0] == "ffprobe":
            if "-select_streams" in args:
                return json.dumps({"streams": streams}), ""
            return VIDEO_PROBE, ""
        output = Path(args[-1])
        if output == frames / "%03d.jpg":
            for index in range(2):
                (frames / f"{index:03d}.jpg").write_bytes(b"partial")
            stderr = "[graph -1 input from stream 0:2 @ 0x123] Invalid color space\n"
            stderr += "An encoder error was printed\n" * 25
            raise ffmpeg._ProcessError("ffmpeg", 234, stderr)
        if output.name == "%03d.jpg":
            (output.parent / "000.jpg").write_bytes(b"recovered")
        return "", ""

    monkeypatch.setattr(ffmpeg, "_run", run)

    assert await media.extract_frames(source, frames) == 1
    assert list(frames.iterdir()) == [frames / "000.jpg"]
    assert (frames / "000.jpg").read_bytes() == b"recovered"
    assert commands[3][5:-1] == (
        "-map", "0:2", "-c:v", "copy", "-bsf:v",
        "hevc_metadata=matrix_coefficients=2:transfer_characteristics=2",
    )  # fmt: skip
    assert commands[1][5:-1] == commands[4][5:-1]


@needs_ffmpeg
async def test_extracts_valid_jpegs_from_h264_with_reserved_color_metadata(
    media, tmp_path, monkeypatch
):
    original, source = tmp_path / "original.mp4", tmp_path / "reserved.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=4:size=320x240:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(original)],
        check=True,
    )  # fmt: skip
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(original), "-c:v", "copy", "-bsf:v",
         "h264_metadata=matrix_coefficients=3:colour_primaries=3:transfer_characteristics=6",
         str(source)],
        check=True,
    )  # fmt: skip
    failures = []
    run = ffmpeg._run

    async def record_failure(*args):
        try:
            return await run(*args)
        except ffmpeg._ProcessError as error:
            failures.append(error)
            raise

    monkeypatch.setattr(ffmpeg, "_run", record_failure)
    frames = empty_dir(tmp_path / "frames")

    assert await media.extract_frames(source, frames) == 4
    assert sorted(path.name for path in frames.iterdir()) == [f"{i:03d}.jpg" for i in range(4)]
    decoded = subprocess.check_output(
        ["ffprobe", "-v", "error", "-f", "image2", "-count_frames", "-show_entries",
         "stream=codec_name,nb_read_frames", "-of", "json", "-i", str(frames / "%03d.jpg")],
    )  # fmt: skip
    assert json.loads(decoded)["streams"] == [{"codec_name": "mjpeg", "nb_read_frames": "4"}]
    if not failures:
        pytest.skip("本机 FFmpeg 原生接受保留色彩矩阵，无法触发回退")
    [first_failure] = failures
    assert "Invalid color space" in first_failure.stderr


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
