"""Tests for AudiobookReprocessor: finding and fixing files stuck in staging."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import AudiobookMetadata, ChannelConfig, MediaType
from src.downloader.reprocessor import AudiobookReprocessor, ReprocessSummary, find_stuck_files
from src.storage.state import StateStore


def make_channel(name: str = "chan", **overrides: object) -> ChannelConfig:
    defaults: dict[str, object] = dict(
        id=f"@{name}",
        name=name,
        media_types=[MediaType.AUDIO],
        output_subdir=name,
        audiobook_mode=True,
        metadata=AudiobookMetadata(author="Some Author", novel_title="Some Novel"),
    )
    defaults.update(overrides)
    return ChannelConfig.model_validate(defaults)


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


# -- find_stuck_files ---------------------------------------------------


def test_find_stuck_files_returns_empty_when_staging_dir_missing(tmp_path: Path) -> None:
    channel = make_channel(output_subdir="missing_staging")
    assert find_stuck_files(tmp_path, channel) == []


def test_find_stuck_files_lists_sorted_files_only(tmp_path: Path) -> None:
    channel = make_channel(output_subdir="staging")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "b.mp3").write_bytes(b"b")
    (staging / "a.mp3").write_bytes(b"a")
    (staging / "subdir").mkdir()

    assert [p.name for p in find_stuck_files(tmp_path, channel)] == ["a.mp3", "b.mp3"]


# -- AudiobookReprocessor.run --------------------------------------------


async def test_run_reprocesses_stuck_file_with_matching_record(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel(output_subdir="staging")
    staging = tmp_path / "staging"
    staging.mkdir()
    stuck_file = staging / "5.mp3"
    stuck_file.write_bytes(b"\x00" * 256)

    await state_store.record_downloaded_file(1, 100, stuck_file)

    dest_root = tmp_path / "Audiobooks"
    reprocessor = AudiobookReprocessor(
        state_store=state_store, download_root=tmp_path, audiobooks_dest_dir=dest_root
    )

    summary = await reprocessor.run([channel])

    assert summary == ReprocessSummary(processed=1, skipped=0, errors=0)
    assert not stuck_file.exists()  # moved out of staging

    new_record_path = dest_root / "Some Author" / "Some Novel" / "Some Novel - Ep 0005.mp3"
    assert new_record_path.exists()

    # State corrected to point at the new location.
    assert await state_store.find_downloaded_record_by_path(stuck_file) is None
    assert await state_store.find_downloaded_record_by_path(new_record_path) == (1, 100)
    state_store.close()


async def test_run_skips_stuck_file_with_no_matching_record(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel(output_subdir="staging")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "5.mp3").write_bytes(b"\x00" * 256)
    # Deliberately no record_downloaded_file call for this path.

    reprocessor = AudiobookReprocessor(
        state_store=state_store,
        download_root=tmp_path,
        audiobooks_dest_dir=tmp_path / "Audiobooks",
    )

    summary = await reprocessor.run([channel])

    assert summary == ReprocessSummary(processed=0, skipped=1, errors=0)
    assert (staging / "5.mp3").exists()  # left in place, untouched
    state_store.close()


async def test_run_counts_error_and_continues_on_unsupported_extension(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel(output_subdir="staging")
    staging = tmp_path / "staging"
    staging.mkdir()
    bad_file = staging / "cover.jpg"
    bad_file.write_bytes(b"not audio")
    await state_store.record_downloaded_file(1, 1, bad_file)

    good_file = staging / "5.mp3"
    good_file.write_bytes(b"\x00" * 256)
    await state_store.record_downloaded_file(1, 2, good_file)

    reprocessor = AudiobookReprocessor(
        state_store=state_store,
        download_root=tmp_path,
        audiobooks_dest_dir=tmp_path / "Audiobooks",
    )

    summary = await reprocessor.run([channel])

    assert summary == ReprocessSummary(processed=1, skipped=0, errors=1)
    assert bad_file.exists()  # untouched after the failure
    state_store.close()


async def test_run_infers_sequential_episode_numbers_across_multiple_stuck_files(
    tmp_path: Path, state_store: StateStore
) -> None:
    # Filenames with no parsable number at all must fall back to
    # "highest existing episode + 1", accumulating correctly across a
    # single reprocess run (sequential processing, no races).
    channel = make_channel(output_subdir="staging")
    staging = tmp_path / "staging"
    staging.mkdir()
    first = staging / "untitled_a.mp3"
    second = staging / "untitled_b.mp3"
    first.write_bytes(b"\x00" * 256)
    second.write_bytes(b"\x00" * 256)
    await state_store.record_downloaded_file(1, 1, first)
    await state_store.record_downloaded_file(1, 2, second)

    dest_root = tmp_path / "Audiobooks"
    reprocessor = AudiobookReprocessor(
        state_store=state_store, download_root=tmp_path, audiobooks_dest_dir=dest_root
    )

    summary = await reprocessor.run([channel])

    assert summary == ReprocessSummary(processed=2, skipped=0, errors=0)
    book_dir = dest_root / "Some Author" / "Some Novel"
    assert (book_dir / "Some Novel - Ep 0001.mp3").exists()
    assert (book_dir / "Some Novel - Ep 0002.mp3").exists()
    state_store.close()
