import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.factories import SERVICE_TOKEN
from video_analysis.core.config.settings import WorkerSettings
from video_analysis.core.storage.workspace import Workspace
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
def media(monkeypatch) -> FfmpegMedia:
    monkeypatch.delenv("VA_FRAME_COUNT", raising=False)
    settings = WorkerSettings(service_token=SERVICE_TOKEN, _env_file=None)
    return FfmpegMedia(frame_count=settings.frame_count, segment_seconds=2)


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
    assert await media.extract_frames(source, frames) == 6
    assert sorted(path.name for path in frames.iterdir()) == [f"{i:03d}.jpg" for i in range(6)]
    assert [call.args[0] for call in run.await_args_list] == ["ffprobe", "ffmpeg"]

    audio = empty_dir(tmp_path / "audio")
    segments = await media.extract_audio(source, audio)
    assert segments == 3
    assert sorted(path.name for path in audio.iterdir()) == [f"{i:03d}.mp3" for i in range(3)]


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

    assert await media.extract_frames(source, frames) == 6
    assert sorted(path.name for path in frames.iterdir()) == [f"{i:03d}.jpg" for i in range(6)]
    decoded = subprocess.check_output(
        ["ffprobe", "-v", "error", "-f", "image2", "-count_frames", "-show_entries",
         "stream=codec_name,nb_read_frames", "-of", "json", "-i", str(frames / "%03d.jpg")],
    )  # fmt: skip
    assert json.loads(decoded)["streams"] == [{"codec_name": "mjpeg", "nb_read_frames": "6"}]
    if not failures:
        pytest.skip("本机 FFmpeg 原生接受保留色彩矩阵，无法触发回退")
    [first_failure] = failures
    assert "Invalid color space" in first_failure.stderr


@needs_ffmpeg
@pytest.mark.parametrize(
    ("audio_filter", "count"),
    [
        ("anullsrc=r=16000:cl=mono:duration=5", 0),
        ("anoisesrc=amplitude=0.00031623:duration=5:seed=42", 0),  # 峰值约 -70 dBFS
        ("sine=frequency=440:duration=5,volume=-12dB", 3),  # 峰值约 -30 dBFS
    ],
    ids=["silence", "near_silence", "quiet_audio"],
)
async def test_audio_volume_thresholds(media, tmp_path, audio_filter, count):
    source = make_video(tmp_path / "source", audio_filter)
    audio = empty_dir(tmp_path / "audio")
    (audio / "009.mp3").write_bytes(b"stale")

    assert await media.extract_audio(source, audio) == count
    assert sorted(path.name for path in audio.iterdir()) == [f"{i:03d}.mp3" for i in range(count)]


@needs_ffmpeg
async def test_drops_tail_shorter_than_half_a_second(media, tmp_path, caplog):
    source = make_video(tmp_path / "source", "sine=frequency=440:duration=2.2")
    audio = empty_dir(tmp_path / "audio")

    assert await media.extract_audio(source, audio) == 1
    assert list(audio.iterdir()) == [audio / "000.mp3"]
    assert "解码后音频不足 0.5 秒" in caplog.text


@needs_ffmpeg
@pytest.mark.parametrize("damage", ["silent", "tiny", "corrupt", "missing", "no_samples"])
async def test_keeps_numbering_contiguous_after_discarding_a_middle_segment(
    media, tmp_path, monkeypatch, caplog, damage
):
    audio_filter = "sine=frequency=440:duration=5"
    if damage == "silent":
        audio_filter += ",volume=0:enable='between(t,1.9,4.1)'"
    source = make_video(tmp_path / "source", audio_filter)
    audio = empty_dir(tmp_path / "audio")
    for index in (0, 1, 2, 9):
        (audio / f"{index:03d}.mp3").write_bytes(b"stale")
    originals = []
    run = ffmpeg._run

    async def damage_after_cut(*args):
        if (
            damage == "no_samples"
            and "volumedetect" in args
            and Path(args[args.index("-i") + 1]).name == "001.mp3"
        ):
            return "", "n_samples: 0\nmax_volume: -20.0 dB"
        result = await run(*args)
        if "-segment_time" in args:
            scratch = Path(args[-1]).parent
            originals.extend((scratch / name).read_bytes() for name in ("000.mp3", "002.mp3"))
            middle = scratch / "001.mp3"
            if damage == "tiny":
                middle.write_bytes(b"ID3")
            elif damage == "corrupt":
                middle.write_bytes(b"not an MP3" * 200)
            elif damage == "missing":
                middle.unlink()
        return result

    monkeypatch.setattr(ffmpeg, "_run", damage_after_cut)

    assert await media.extract_audio(source, audio) == 2
    published = sorted(audio.glob("*.mp3"))
    assert [path.name for path in published] == ["000.mp3", "001.mp3"]
    assert [path.read_bytes() for path in published] == originals
    assert Workspace.segment_indices(audio, 2) == [0, 2]
    if damage != "missing":
        assert "001.mp3" in caplog.text


@needs_ffmpeg
async def test_audio_decode_failure_does_not_prevent_six_video_frames(media, tmp_path, caplog):
    source = make_video(tmp_path / "source", "sine=frequency=440:duration=5")
    output = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "packet=pos,size", "-of", "json", str(source)],
    )  # fmt: skip
    data = bytearray(source.read_bytes())
    packets = json.loads(output)["packets"]
    assert packets
    for packet in packets:
        pos, size = int(packet["pos"]), int(packet["size"])
        data[pos : pos + size] = b"\xff" * size
    source.write_bytes(data)

    assert (await media.probe(source)).has_audio
    audio = empty_dir(tmp_path / "audio")
    assert await media.extract_audio(source, audio) == 0
    assert list(audio.iterdir()) == []
    assert "音轨预检失败" in caplog.text
    assert await media.extract_frames(source, empty_dir(tmp_path / "frames")) == 6


@pytest.mark.parametrize(
    "stats", ["", "n_samples: 0\nmax_volume: -20.0 dB", "n_samples: 16000\nmax_volume: N/A dB"]
)
async def test_preflight_requires_samples_and_peak_volume(
    media, tmp_path, monkeypatch, caplog, stats
):
    run = AsyncMock(return_value=("", stats))
    monkeypatch.setattr(ffmpeg, "_run", run)
    audio = empty_dir(tmp_path / "audio")

    assert await media.extract_audio(tmp_path / "source", audio) == 0
    assert list(audio.iterdir()) == []
    assert "未检测到有效的音频采样或峰值音量" in caplog.text
    assert run.await_count == 1
    assert "volumedetect" in run.await_args.args


@pytest.mark.parametrize(("peak", "discard"), [(-52.0, True), (-30.0, False)])
async def test_low_average_volume_only_discards_segments_with_quiet_peaks(
    tmp_path, monkeypatch, peak, discard
):
    segment = tmp_path / "000.mp3"
    segment.write_bytes(b"\x00" * 1024)
    probe = {
        "streams": [{"codec_name": "mp3", "sample_rate": "16000"}],
        "frames": [{"nb_samples": 576}] * 14,
    }
    stats = f"n_samples: 8064\nmean_volume: -67.0 dB\nmax_volume: {peak} dB"
    run = AsyncMock(side_effect=[(json.dumps(probe), ""), ("", stats)])
    monkeypatch.setattr(ffmpeg, "_run", run)

    issue = await ffmpeg._audio_segment_issue(segment)
    assert (issue is not None) == discard


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
