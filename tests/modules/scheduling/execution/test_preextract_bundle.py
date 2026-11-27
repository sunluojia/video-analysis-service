import hashlib
import json
import tarfile
from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from tests.factories import accept, configure_ai, emitted, events, load_job, video_message
from tests.modules.scheduling.execution.fakes import Setup
from video_analysis.integrations.media.bundle import (
    InvalidPreextractBundle,
    is_preextract_bundle,
    open_preextract_bundle,
)
from video_analysis.modules.scheduling.task.models import Task, TaskKind

JPEG = b"\xff\xd8\xff\xd9"
MP3 = b"ID3bundle audio"
BUNDLE_ID = "pre-extract archive"


def member(name, content, kind=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.type = kind
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = "frames/000.jpg"
    return info, BytesIO(content)


def make_bundle(*, has_audio=True, indices=(0, 2), extra=(), **fields):
    manifest = {
        "schema": "video-preextract-v1",
        "duration": 140.0,
        "has_audio": has_audio,
        "frame_count": 1,
        "frame_count_config": 6,
        "segment_seconds_config": 60,
        "audio_indices": list(indices),
        "original_size": 8000,
        "original_sha256": "f" * 64,
    } | fields
    payload = BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        archive.addfile(*member("manifest.json", json.dumps(manifest).encode()))
        archive.addfile(*member("frames/000.jpg", JPEG))
        for published, original in enumerate(indices):
            archive.addfile(*member(f"audio/{published:03d}.mp3", MP3 + bytes([original])))
        for entry in extra:
            archive.addfile(*entry)
    return payload.getvalue()


async def accept_bundle(database, setup: Setup, payload: bytes):
    fallback = setup.materials.download

    async def download(material, target):
        if material.id != BUNDLE_ID:
            await fallback(material, target)
            return
        assert material.size == len(payload)
        assert material.sha256 == hashlib.sha256(payload).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    setup.materials.download = download
    message = video_message("bundle", content=BUNDLE_ID.encode())
    message.material.size = len(payload)
    message.material.sha256 = hashlib.sha256(payload).hexdigest()
    await accept(database, message)


@pytest.mark.parametrize(("has_audio", "indices"), [(True, (0, 2)), (False, ()), (True, ())])
async def test_bundle_completes_without_ffmpeg(
    database, workspace, monkeypatch, has_audio, indices
):
    setup = Setup(database, workspace)
    forbidden = AsyncMock(side_effect=AssertionError("FFmpeg called for a bundle"))
    for method in ("probe", "extract_frames", "extract_audio"):
        monkeypatch.setattr(setup.media, method, forbidden)
    await configure_ai(database)
    await accept_bundle(database, setup, make_bundle(has_audio=has_audio, indices=indices))
    await setup.run_until(emitted(database, type="job", status="completed"))

    forbidden.assert_not_awaited()
    job = await load_job(database, "bundle")
    assert not workspace.source(job.id).exists()
    assert workspace.frame(job.id, 0).read_bytes() == JPEG
    succeeded = await events(database, type="stage", status="succeeded")
    assert next(entry for entry in succeeded if entry["stage"] == "fetch")["result"] == {
        "size": job.video.material.size,
        "sha256": job.video.material.sha256,
        "duration": 140.0,
        "has_audio": has_audio,
    }
    assert sum(entry["stage"] == "listen" for entry in succeeded) == len(indices)
    assert any(entry["stage"] == "recognize" for entry in succeeded)
    [audio] = await events(
        database,
        type="stage",
        stage="extract_audio",
        status=("succeeded" if has_audio else "skipped"),
    )
    if indices:
        assert [(part["start"], part["end"]) for part in audio["result"]["segments"]] == [
            (0, 60),
            (120, 140),
        ]
        for published, original in enumerate(indices):
            assert workspace.segment(job.id, published).read_bytes() == MP3 + bytes([original])
    elif has_audio:
        assert audio["result"]["segment_count"] == 0
    else:
        assert "skip_reason" in audio["result"]


async def test_bundle_retries_and_mp4_works_in_same_batch(database, workspace, monkeypatch):
    setup = Setup(database, workspace)
    await configure_ai(database)
    await accept_bundle(database, setup, make_bundle())
    job = await load_job(database, "bundle")
    pipeline = setup.pipeline
    await pipeline._process_media(Task(TaskKind.FETCH, job.id))
    assert is_preextract_bundle(workspace.source(job.id))
    for _ in range(2):
        assert await pipeline._process_media(Task(TaskKind.EXTRACT_FRAMES, job.id)) == 1
        result = await pipeline._process_media(Task(TaskKind.EXTRACT_AUDIO, job.id))
        assert result.indices == workspace.segment_indices(workspace.segment(job.id, 0).parent, 2)
        assert result.indices == [0, 2]
        assert workspace.source(job.id).is_file()
        workspace.frame(job.id, 1).write_bytes(b"stale")
        workspace.segment(job.id, 2).write_bytes(b"stale")

    for method in ("probe", "extract_frames", "extract_audio"):
        spy = AsyncMock(wraps=getattr(setup.media, method))
        monkeypatch.setattr(setup.media, method, spy)
    await accept(database, video_message("mp4", content=b"0000ftyp sample"))

    async def both_completed():
        return len(await events(database, type="job", status="completed")) == 2

    await setup.run_until(both_completed)
    for method in ("probe", "extract_frames", "extract_audio"):
        getattr(setup.media, method).assert_awaited_once()
    assert not workspace.frame(job.id, 1).exists()
    assert not workspace.segment(job.id, 2).exists()


@pytest.mark.parametrize(
    ("name", "kind", "data"),
    [
        ("../outside.jpg", tarfile.REGTYPE, b"x"),
        ("/frames/001.jpg", tarfile.REGTYPE, b"x"),
        (r"frames\..\outside.jpg", tarfile.REGTYPE, b"x"),
        ("frames/001.jpg", tarfile.SYMTYPE, b""),
        ("frames/001.jpg", tarfile.LNKTYPE, b""),
        ("frames/001.jpg", tarfile.CHRTYPE, b""),
        ("frames/000.jpg", tarfile.REGTYPE, b"duplicate"),
        ("frames/001.jpg", tarfile.REGTYPE, b""),
        ("frames/099.jpg", tarfile.REGTYPE, b"unexpected"),
    ],
)
def test_rejects_unsafe_or_invalid_tar_members(tmp_path, name, kind, data):
    source = tmp_path / "source"
    source.write_bytes(make_bundle(extra=[member(name, data, kind)]))
    assert is_preextract_bundle(source)
    with pytest.raises(InvalidPreextractBundle), open_preextract_bundle(source):
        pytest.fail("invalid tar was accepted")
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize(
    "fields",
    [
        {"frame_count": 0},
        {"frame_count": 2},
        {"frame_count": True},
        {"has_audio": False},
        {"audio_indices": [False, 2]},
        {"audio_indices": [2, 2]},
        {"audio_indices": [2, 0]},
        {"schema": "unexpected"},
        {"original_sha256": "bad"},
        {"duration": float("inf")},
    ],
)
def test_rejects_invalid_manifest_fields(tmp_path, fields):
    source = tmp_path / "source"
    if fields == {"has_audio": False}:
        source.write_bytes(make_bundle(has_audio=False))
    else:
        source.write_bytes(make_bundle(**fields))
    with pytest.raises(InvalidPreextractBundle), open_preextract_bundle(source):
        pytest.fail("invalid manifest was accepted")


def test_rejects_truncation_and_invalid_headers(tmp_path):
    source = tmp_path / "source"
    payload = make_bundle()
    invalid_header = payload[:1024] + b"broken" + payload[1030:]
    for data in (payload[:1750], invalid_header, payload[:-1] + b"x"):
        source.write_bytes(data)
        with pytest.raises(InvalidPreextractBundle), open_preextract_bundle(source):
            pytest.fail("corrupt tar was accepted")


def test_mp4_is_not_a_bundle(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 600)
    assert not is_preextract_bundle(source)
    with open_preextract_bundle(source) as bundle:
        assert bundle is None
