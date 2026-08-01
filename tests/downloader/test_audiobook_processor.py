"""Tests for audiobook post-processing: extraction, ID3 tagging, and moves."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from src.config.settings import AudiobookMetadata
from src.downloader.audiobook_processor import (
    EpisodeInfo,
    build_destination_path,
    extract_episode_info,
    format_title,
    process_audiobook_file,
    tag_audio_file,
)

REAL_WORLD_FILENAME = "__Shadow Slave.Ep 2027 - The Strength of the Wolf-XtreamStories.mp3"


def _write_dummy_mp3(path: Path) -> None:
    # EasyID3/ID3 write a tag header regardless of whether the rest of the
    # file is valid MPEG audio, so arbitrary bytes are sufficient here.
    path.write_bytes(b"\x00" * 256)


# -- extract_episode_info -----------------------------------------------


def test_extracts_episode_and_subtitle_from_real_world_filename() -> None:
    info = extract_episode_info(REAL_WORLD_FILENAME, fallback_episode=999)

    assert info.episode == 2027
    assert info.subtitle == "The Strength of the Wolf"


def test_extraction_is_case_insensitive_and_accepts_episode_word() -> None:
    info = extract_episode_info("Episode 12 - The Beginning.mp3", fallback_episode=1)
    assert info.episode == 12
    assert info.subtitle == "The Beginning"

    info2 = extract_episode_info("ep.5 - Something.mp3", fallback_episode=1)
    assert info2.episode == 5
    assert info2.subtitle == "Something"


def test_extraction_handles_colon_separator() -> None:
    info = extract_episode_info("Ep 42: The Return.mp3", fallback_episode=1)
    assert info.episode == 42
    assert info.subtitle == "The Return"


def test_extraction_falls_back_to_message_id_when_no_pattern_matches() -> None:
    info = extract_episode_info("random_upload_name.mp3", fallback_episode=54321)
    assert info.episode == 54321
    assert info.subtitle is None


def test_extraction_preserves_subtitle_with_space_separated_trailing_number() -> None:
    # "Part 2" is genuine subtitle content (space before the digit), not an
    # uploader signature, and must not be stripped.
    info = extract_episode_info("Ep 7 - Final Battle Part 2.mp3", fallback_episode=1)
    assert info.subtitle == "Final Battle Part 2"


def test_extraction_with_no_subtitle_text() -> None:
    info = extract_episode_info("Ep 9 - .mp3", fallback_episode=1)
    assert info.episode == 9
    assert info.subtitle is None


# -- format_title ---------------------------------------------------------


def test_format_title_uses_subtitle_when_present() -> None:
    info = EpisodeInfo(episode=2027, subtitle="The Strength of the Wolf")
    assert format_title("Shadow Slave", info) == "Ep 2027 - The Strength of the Wolf"


def test_format_title_falls_back_to_novel_title_when_subtitle_missing() -> None:
    info = EpisodeInfo(episode=9, subtitle=None)
    assert format_title("Shadow Slave", info) == "Shadow Slave - Ep 9"


# -- build_destination_path ------------------------------------------------


def test_build_destination_path_layout(tmp_path: Path) -> None:
    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")
    info = EpisodeInfo(episode=2027, subtitle="The Strength of the Wolf")

    destination = build_destination_path(tmp_path, metadata, info, ".mp3")

    assert destination == (
        tmp_path
        / "Guiltythree"
        / "Shadow Slave"
        / "Shadow Slave - Ep 2027 - The Strength of the Wolf.mp3"
    )


def test_build_destination_path_omits_subtitle_when_absent(tmp_path: Path) -> None:
    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")
    info = EpisodeInfo(episode=9, subtitle=None)

    destination = build_destination_path(tmp_path, metadata, info, ".mp3")

    assert destination.name == "Shadow Slave - Ep 0009.mp3"


def test_build_destination_path_pads_episode_number(tmp_path: Path) -> None:
    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")
    info = EpisodeInfo(episode=7, subtitle="Intro")

    destination = build_destination_path(tmp_path, metadata, info, ".mp3")

    assert "Ep 0007" in destination.name


def test_build_destination_path_sanitizes_unsafe_author_and_title(tmp_path: Path) -> None:
    metadata = AudiobookMetadata(author="Author: <Evil>", novel_title="Title/With\\Slashes")
    info = EpisodeInfo(episode=1, subtitle=None)

    destination = build_destination_path(tmp_path, metadata, info, ".mp3")

    for part in destination.relative_to(tmp_path).parts:
        assert "/" not in part and "\\" not in part and ":" not in part and "<" not in part


# -- tag_audio_file (mutagen) -----------------------------------------------


def test_tag_audio_file_writes_expected_mp3_id3_fields(tmp_path: Path) -> None:
    path = tmp_path / "chapter.mp3"
    _write_dummy_mp3(path)

    tag_audio_file(
        path,
        author="Guiltythree",
        novel_title="Shadow Slave",
        episode=2027,
        title="Ep 2027 - The Strength of the Wolf",
    )

    tags = EasyID3(path)  # type: ignore[no-untyped-call]
    assert tags["artist"] == ["Guiltythree"]
    assert tags["albumartist"] == ["Guiltythree"]
    assert tags["album"] == ["Shadow Slave"]
    assert tags["title"] == ["Ep 2027 - The Strength of the Wolf"]
    assert tags["tracknumber"] == ["2027"]


def test_tag_audio_file_rejects_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "cover.jpg"
    path.write_bytes(b"not audio")

    with pytest.raises(ValueError):
        tag_audio_file(path, author="A", novel_title="B", episode=1, title="C")


# -- process_audiobook_file (full pipeline) ---------------------------------


async def test_process_audiobook_file_tags_and_moves_into_author_title_layout(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    dest_root = tmp_path / "Audiobooks"

    source = staging_dir / REAL_WORLD_FILENAME
    _write_dummy_mp3(source)

    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")

    result_path = await process_audiobook_file(
        source, message_id=999999, metadata=metadata, dest_root=dest_root
    )

    assert result_path == (
        dest_root
        / "Guiltythree"
        / "Shadow Slave"
        / "Shadow Slave - Ep 2027 - The Strength of the Wolf.mp3"
    )
    assert result_path.exists()
    assert not source.exists()  # moved, not copied

    tags = EasyID3(result_path)  # type: ignore[no-untyped-call]
    assert tags["artist"] == ["Guiltythree"]
    assert tags["album"] == ["Shadow Slave"]
    assert tags["tracknumber"] == ["2027"]
    assert tags["title"] == ["Ep 2027 - The Strength of the Wolf"]


async def test_process_audiobook_file_falls_back_to_message_id_when_untitled(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source = staging_dir / "12345_67890.mp3"
    _write_dummy_mp3(source)

    metadata = AudiobookMetadata(author="Some Author", novel_title="Untitled Series")

    result_path = await process_audiobook_file(
        source, message_id=67890, metadata=metadata, dest_root=tmp_path / "Audiobooks"
    )

    assert result_path.name == "Untitled Series - Ep 67890.mp3"
    tags = EasyID3(result_path)  # type: ignore[no-untyped-call]
    assert tags["tracknumber"] == ["67890"]
    assert tags["title"] == ["Untitled Series - Ep 67890"]


async def test_process_audiobook_file_rejects_unsupported_extension(tmp_path: Path) -> None:
    source = tmp_path / "cover.jpg"
    source.write_bytes(b"not audio")
    metadata = AudiobookMetadata(author="A", novel_title="B")

    with pytest.raises(ValueError):
        await process_audiobook_file(
            source, message_id=1, metadata=metadata, dest_root=tmp_path / "Audiobooks"
        )


async def test_process_audiobook_file_avoids_overwriting_existing_destination(
    tmp_path: Path,
) -> None:
    dest_root = tmp_path / "Audiobooks"
    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")

    existing = dest_root / "Guiltythree" / "Shadow Slave" / "Shadow Slave - Ep 0001.mp3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"original content")

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source = staging_dir / "chapter1.mp3"
    _write_dummy_mp3(source)

    result_path = await process_audiobook_file(
        source, message_id=1, metadata=metadata, dest_root=dest_root
    )

    assert result_path != existing
    assert result_path.name == "Shadow Slave - Ep 0001 (1).mp3"
    assert existing.read_bytes() == b"original content"  # untouched
