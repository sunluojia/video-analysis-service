"""Read an uncompressed video-preextract-v1 tar without restoring archive metadata."""

import asyncio
import json
import os
import re
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from video_analysis.core.storage.workspace import AUDIO_SEGMENT_INDICES

_SCHEMA = "video-preextract-v1"
_MANIFEST = "manifest.json"
_MAX_MANIFEST_BYTES = 64 * 1024
_MEMBER_NAME = re.compile(r"manifest\.json|frames/[0-9]{3}\.jpg|audio/[0-9]{3,}\.mp3")
_COPY_BYTES = 1024 * 1024
_BLOCK = tarfile.BLOCKSIZE
_ZERO_BLOCK = bytes(_BLOCK)


class InvalidPreextractBundle(ValueError):
    """The source has a tar signature but violates the pre-extract format."""


class BundleManifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    schema_id: Literal["video-preextract-v1"] = Field(alias="schema")
    duration: float = Field(gt=0)
    has_audio: bool
    frame_count: int = Field(ge=1, le=1000)
    frame_count_config: int = Field(ge=1, le=1000)
    segment_seconds_config: int = Field(gt=0)
    audio_indices: list[Annotated[int, Field(ge=0)]]
    original_size: int = Field(gt=0)
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@contextmanager
def _open_tar(source: Path) -> Iterator[BinaryIO | None]:
    with source.open("rb") as file:
        header = file.read(_BLOCK)
        if header[4:8] == b"ftyp" or header[257:262] != b"ustar":
            yield None
        else:
            file.seek(0)
            yield file


def _members(file: BinaryIO) -> Iterator[tarfile.TarInfo]:
    size = os.fstat(file.fileno()).st_size
    while True:
        header = file.read(_BLOCK)
        if header == _ZERO_BLOCK:
            if file.read(_BLOCK) != _ZERO_BLOCK or any(
                padding.strip(b"\x00") for padding in iter(lambda: file.read(_COPY_BYTES), b"")
            ):
                raise InvalidPreextractBundle("Invalid end of pre-extract archive")
            return
        try:
            member = tarfile.TarInfo.frombuf(header, "ascii", "strict")
        except (tarfile.HeaderError, ValueError) as error:
            raise InvalidPreextractBundle("Corrupt pre-extract tar header") from error
        regular = (
            member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
            and member.size > 0
            and _MEMBER_NAME.fullmatch(member.name) is not None
        )
        directory = (
            member.type == tarfile.DIRTYPE
            and member.name in {"frames", "audio"}
            and member.size == 0
        )
        if not (regular or directory):
            raise InvalidPreextractBundle("Unsupported path or member type in pre-extract archive")
        # Only a header is parsed. Extension headers, links and devices never reach disk.
        member.offset_data = file.tell()
        end = member.offset_data + ((member.size + _BLOCK - 1) // _BLOCK) * _BLOCK
        if end > size:
            raise InvalidPreextractBundle("Truncated member in pre-extract archive")
        yield member
        file.seek(end)


def _read_manifest(file: BinaryIO, member: tarfile.TarInfo) -> object:
    if member.size > _MAX_MANIFEST_BYTES:
        raise InvalidPreextractBundle("Pre-extract manifest exceeds 64 KiB")
    file.seek(member.offset_data)
    try:
        return json.loads(file.read(member.size), object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as error:
        raise InvalidPreextractBundle("Invalid pre-extract manifest JSON") from error


def _unique_fields(entries: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(entries)
    if len(fields) != len(entries):
        raise ValueError("Duplicate manifest key")
    return fields


def is_preextract_bundle(path: Path) -> bool:
    """Check only tar headers up to the manifest and its schema ID."""
    try:
        with _open_tar(path) as file:
            if file is not None:
                for member in _members(file):
                    if member.name == _MANIFEST:
                        data = _read_manifest(file, member)
                        return isinstance(data, dict) and data.get("schema") == _SCHEMA
    except InvalidPreextractBundle:
        return False
    return False


class PreextractBundle:
    def __init__(self, file: BinaryIO) -> None:
        self._file = file
        members: dict[str, tarfile.TarInfo] = {}
        for member in _members(file):
            if member.name in members:
                raise InvalidPreextractBundle("Duplicate member in pre-extract archive")
            members[member.name] = member
        if _MANIFEST not in members:
            raise InvalidPreextractBundle("Missing pre-extract manifest")
        try:
            manifest = BundleManifest.model_validate(_read_manifest(file, members[_MANIFEST]))
        except ValidationError as error:
            raise InvalidPreextractBundle("Invalid pre-extract manifest fields") from error
        indices = manifest.audio_indices
        if manifest.frame_count > manifest.frame_count_config:
            raise InvalidPreextractBundle("Frame count exceeds the configured count")
        if (indices and not manifest.has_audio) or indices != sorted(set(indices)):
            raise InvalidPreextractBundle("Invalid original audio indices")

        frames = [f"frames/{index:03d}.jpg" for index in range(manifest.frame_count)]
        audio = [f"audio/{index:03d}.mp3" for index in range(len(indices))]
        if {_MANIFEST, *frames, *audio} != {
            name for name, member in members.items() if member.isfile()
        }:
            raise InvalidPreextractBundle("Archive members do not match the pre-extract manifest")
        self.manifest = manifest
        self._frames = [members[name] for name in frames]
        self._audio = [members[name] for name in audio]

    async def extract_frames(self, target: Path) -> int:
        await self._copy(self._frames, target)
        return self.manifest.frame_count

    async def extract_audio(self, target: Path) -> list[int]:
        await self._copy(self._audio, target)
        indices = self.manifest.audio_indices
        if indices != list(range(len(indices))):
            (target / AUDIO_SEGMENT_INDICES).write_text(json.dumps(indices), encoding="utf-8")
        return indices

    async def _copy(self, members: list[tarfile.TarInfo], target: Path) -> None:
        for member in members:
            self._file.seek(member.offset_data)
            remaining = member.size
            with (target / member.name.split("/", 1)[1]).open("wb") as file:
                while remaining:
                    payload = self._file.read(min(remaining, _COPY_BYTES))
                    if not payload:
                        raise InvalidPreextractBundle("Truncated member in pre-extract archive")
                    file.write(payload)
                    remaining -= len(payload)
                    await asyncio.sleep(0)


@contextmanager
def open_preextract_bundle(source: Path) -> Iterator[PreextractBundle | None]:
    """Return a fully validated bundle, or None for ordinary media."""
    with _open_tar(source) as file:
        yield PreextractBundle(file) if file is not None else None
