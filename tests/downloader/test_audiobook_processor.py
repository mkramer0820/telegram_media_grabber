"""Tests for audiobook post-processing: extraction, ID3 tagging, and moves."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from src.config.settings import AudiobookMetadata
from src.downloader.audiobook_processor import (
    EpisodeInfo,
    apply_episode_tagging,
    book_dir,
    build_destination_path,
    extract_episode_info,
    format_title,
    infer_next_episode_number,
    parse_tagged_episode_number,
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
    info = extract_episode_info(REAL_WORLD_FILENAME)

    assert info is not None
    assert info.episode == 2027
    assert info.subtitle == "The Strength of the Wolf"


def test_extraction_is_case_insensitive_and_accepts_episode_word() -> None:
    info = extract_episode_info("Episode 12 - The Beginning.mp3")
    assert info is not None
    assert info.episode == 12
    assert info.subtitle == "The Beginning"

    info2 = extract_episode_info("ep.5 - Something.mp3")
    assert info2 is not None
    assert info2.episode == 5
    assert info2.subtitle == "Something"


def test_extraction_handles_colon_separator() -> None:
    info = extract_episode_info("Ep 42: The Return.mp3")
    assert info is not None
    assert info.episode == 42
    assert info.subtitle == "The Return"


def test_extraction_returns_none_when_no_pattern_matches() -> None:
    assert extract_episode_info("random_upload_name.mp3") is None
    assert extract_episode_info("nonumbershere.mp3") is None


def test_extraction_preserves_subtitle_with_space_separated_trailing_number() -> None:
    # "Part 2" is genuine subtitle content (space before the digit), not an
    # uploader signature, and must not be stripped.
    info = extract_episode_info("Ep 7 - Final Battle Part 2.mp3")
    assert info is not None
    assert info.subtitle == "Final Battle Part 2"


def test_extraction_with_no_subtitle_text() -> None:
    info = extract_episode_info("Ep 9 - .mp3")
    assert info is not None
    assert info.episode == 9
    assert info.subtitle is None


def test_extraction_parses_bare_numeric_filename() -> None:
    # Some channels post chapters as just a number, no "Ep" prefix at all.
    info = extract_episode_info("1114.m4a")
    assert info is not None
    assert info.episode == 1114
    assert info.subtitle is None


def test_extraction_parses_bare_numeric_filename_with_trailing_dot_artifact() -> None:
    # A raw filename of "1114..m4a" leaves Path(...).stem as "1114." (the
    # trailing dot survives since Path only splits on the *last* dot).
    info = extract_episode_info("1114..m4a")
    assert info is not None
    assert info.episode == 1114
    assert info.subtitle is None


def test_extraction_parses_numeric_range_using_start_number() -> None:
    # A bundled multi-chapter file named as a range, e.g. "5-6.m4a".
    info = extract_episode_info("5-6.m4a")
    assert info is not None
    assert info.episode == 5
    assert info.subtitle is None


def test_extraction_parses_trailing_range_with_title_prefix_using_start_number() -> None:
    # A title prefix before a bundled range, e.g. a book title the uploader
    # included in the filename — the number is what matters; author/title
    # metadata always comes from config regardless of this prefix text.
    info = extract_episode_info("Shadow Slave 1751-1846.m4a")
    assert info is not None
    assert info.episode == 1751
    assert info.subtitle is None


def test_extraction_parses_leading_range_with_title_suffix_using_start_number() -> None:
    # A leading range before a title suffix, using "_" as the range
    # separator, e.g. "0001_0100_Weakest_Beast_Tamer.mp3" (chapters 1-100).
    info = extract_episode_info("0001_0100_Weakest_Beast_Tamer.mp3")
    assert info is not None
    assert info.episode == 1
    assert info.subtitle is None


def test_extraction_parses_underscore_separated_range() -> None:
    # "_" and "-" are both accepted as the range separator.
    info = extract_episode_info("0201_0300_Weakest_Beast_Tamer.mp3")
    assert info is not None
    assert info.episode == 201


# -- format_title ---------------------------------------------------------


def test_format_title_uses_subtitle_when_present() -> None:
    info = EpisodeInfo(episode=2027, subtitle="The Strength of the Wolf")
    assert format_title("Shadow Slave", info) == "Ep 2027 - The Strength of the Wolf"


def test_format_title_falls_back_to_novel_title_when_subtitle_missing() -> None:
    info = EpisodeInfo(episode=9, subtitle=None)
    assert format_title("Shadow Slave", info) == "Shadow Slave - Ep 9"


# -- infer_next_episode_number / book_dir -----------------------------------


def test_infer_next_episode_number_is_one_when_dest_dir_missing(tmp_path: Path) -> None:
    assert infer_next_episode_number(tmp_path / "does_not_exist") == 1


def test_infer_next_episode_number_is_one_when_dest_dir_empty(tmp_path: Path) -> None:
    dest_dir = tmp_path / "Shadow Slave"
    dest_dir.mkdir()
    assert infer_next_episode_number(dest_dir) == 1


def test_infer_next_episode_number_is_one_past_highest_existing_episode(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "Shadow Slave"
    dest_dir.mkdir()
    (dest_dir / "Shadow Slave - Ep 0007 - Title.mp3").write_bytes(b"x")
    (dest_dir / "Shadow Slave - Ep 0012.mp3").write_bytes(b"x")
    (dest_dir / "cover.jpg").write_bytes(b"x")  # no "Ep n" -> ignored

    assert infer_next_episode_number(dest_dir) == 13


def test_book_dir_matches_build_destination_path_directory(tmp_path: Path) -> None:
    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")
    info = EpisodeInfo(episode=1, subtitle=None)

    destination = build_destination_path(tmp_path, metadata, info, ".mp3")

    assert destination.parent == book_dir(tmp_path, metadata)


# -- parse_tagged_episode_number ---------------------------------------------


def test_parse_tagged_episode_number_extracts_number() -> None:
    assert parse_tagged_episode_number(Path("Shadow Slave - Ep 0042.mp3")) == 42


def test_parse_tagged_episode_number_extracts_number_with_subtitle() -> None:
    assert parse_tagged_episode_number(Path("Shadow Slave - Ep 0007 - Title.mp3")) == 7


def test_parse_tagged_episode_number_returns_none_when_untagged() -> None:
    assert parse_tagged_episode_number(Path("cover.jpg")) is None


# -- apply_episode_tagging ---------------------------------------------------


async def test_apply_episode_tagging_uses_given_info_not_filenames_own_number(
    tmp_path: Path,
) -> None:
    # The source filename claims episode 999 — apply_episode_tagging must
    # ignore that and use the explicitly-passed EpisodeInfo instead. This is
    # exactly the property src.downloader.episode_verifier relies on to
    # correct a file that's already mistagged under the wrong number.
    source = tmp_path / "staging" / "Ep 999 - Wrong.mp3"
    source.parent.mkdir(parents=True)
    _write_dummy_mp3(source)

    metadata = AudiobookMetadata(author="Guiltythree", novel_title="Shadow Slave")
    correct_info = EpisodeInfo(episode=5, subtitle="The Real One")

    result_path = await apply_episode_tagging(source, correct_info, metadata, tmp_path / "Audiobooks")

    assert result_path.name == "Shadow Slave - Ep 0005 - The Real One.mp3"
    assert not source.exists()
    tags = EasyID3(result_path)  # type: ignore[no-untyped-call]
    assert tags["tracknumber"] == ["5"]


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


async def test_process_audiobook_file_infers_episode_one_when_untitled_and_dest_empty(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source = staging_dir / "totally_untitled_file.mp3"  # no parsable episode number
    _write_dummy_mp3(source)

    metadata = AudiobookMetadata(author="Some Author", novel_title="Untitled Series")

    result_path = await process_audiobook_file(
        # message_id is deliberately unrelated to any real episode count
        # (see CLAUDE.md-adjacent bug notes) — it must NOT end up as the
        # episode number anywhere below.
        source, message_id=67890, metadata=metadata, dest_root=tmp_path / "Audiobooks"
    )

    assert result_path.name == "Untitled Series - Ep 0001.mp3"
    tags = EasyID3(result_path)  # type: ignore[no-untyped-call]
    assert tags["tracknumber"] == ["1"]
    assert tags["title"] == ["Untitled Series - Ep 1"]


async def test_process_audiobook_file_infers_episode_from_existing_dest_files(
    tmp_path: Path,
) -> None:
    dest_root = tmp_path / "Audiobooks"
    metadata = AudiobookMetadata(author="Some Author", novel_title="Untitled Series")
    existing_dir = dest_root / "Some Author" / "Untitled Series"
    existing_dir.mkdir(parents=True)
    (existing_dir / "Untitled Series - Ep 0005.mp3").write_bytes(b"x")

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source = staging_dir / "no_number_here.mp3"
    _write_dummy_mp3(source)

    result_path = await process_audiobook_file(
        source, message_id=1, metadata=metadata, dest_root=dest_root
    )

    assert result_path.name == "Untitled Series - Ep 0006.mp3"


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
    # Bare-numeric filename, explicitly parsed as episode 1 (not inferred) —
    # this deliberately collides with the pre-existing "Ep 0001.mp3" so the
    # test exercises dedup_suffixed_path, not infer_next_episode_number.
    source = staging_dir / "1.mp3"
    _write_dummy_mp3(source)

    result_path = await process_audiobook_file(
        source, message_id=999, metadata=metadata, dest_root=dest_root
    )

    assert result_path != existing
    assert result_path.name == "Shadow Slave - Ep 0001 (1).mp3"
    assert existing.read_bytes() == b"original content"  # untouched
