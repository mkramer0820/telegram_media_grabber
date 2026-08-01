"""Tests for the SQLite-backed StateStore (CLAUDE.md Section 4.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.storage.state import StateStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


async def test_get_last_message_id_defaults_to_none(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        assert await store.get_last_message_id(chat_id=1) is None


async def test_set_and_get_last_message_id_roundtrip(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        await store.set_last_message_id(chat_id=1, message_id=42)
        assert await store.get_last_message_id(chat_id=1) == 42


async def test_set_last_message_id_never_regresses(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        await store.set_last_message_id(chat_id=1, message_id=100)
        await store.set_last_message_id(chat_id=1, message_id=50)
        assert await store.get_last_message_id(chat_id=1) == 100


async def test_last_message_id_is_tracked_independently_per_chat(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        await store.set_last_message_id(chat_id=1, message_id=10)
        await store.set_last_message_id(chat_id=2, message_id=20)

        assert await store.get_last_message_id(chat_id=1) == 10
        assert await store.get_last_message_id(chat_id=2) == 20


async def test_is_downloaded_false_until_recorded(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        assert await store.is_downloaded(chat_id=1, message_id=5) is False
        await store.record_downloaded_file(1, 5, Path("downloads/file.mp4"))
        assert await store.is_downloaded(chat_id=1, message_id=5) is True


async def test_record_downloaded_file_is_idempotent(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        await store.record_downloaded_file(1, 5, Path("downloads/file.mp4"), "hash-a")
        # Re-recording the same (chat_id, message_id) must not raise and
        # must not lose the original record (CLAUDE.md Section 2.4).
        await store.record_downloaded_file(1, 5, Path("downloads/file-renamed.mp4"), "hash-b")

        paths = await store.find_by_content_hash("hash-a")
        assert paths == [Path("downloads/file.mp4")]


async def test_find_by_content_hash_returns_all_matches(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        await store.record_downloaded_file(1, 1, Path("a.mp4"), "shared-hash")
        await store.record_downloaded_file(1, 2, Path("b.mp4"), "shared-hash")
        await store.record_downloaded_file(1, 3, Path("c.mp4"), "different-hash")

        matches = await store.find_by_content_hash("shared-hash")
        assert set(matches) == {Path("a.mp4"), Path("b.mp4")}


async def test_find_by_content_hash_no_match_returns_empty(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        assert await store.find_by_content_hash("nonexistent") == []


async def test_concurrent_writes_are_serialized_without_corruption(db_path: Path) -> None:
    """Many concurrent tasks hammering the same StateStore must not corrupt
    it or raise "database is locked" errors — the internal asyncio.Lock
    (CLAUDE.md Section 4.3) must serialize all writes.
    """
    async with StateStore(db_path) as store:
        async def record(i: int) -> None:
            await store.record_downloaded_file(chat_id=1, message_id=i, file_path=Path(f"{i}.mp4"))
            await store.set_last_message_id(chat_id=1, message_id=i)

        await asyncio.gather(*(record(i) for i in range(200)))

        assert await store.get_last_message_id(chat_id=1) == 199
        for i in range(200):
            assert await store.is_downloaded(chat_id=1, message_id=i) is True


async def test_concurrent_set_last_message_id_converges_to_max(db_path: Path) -> None:
    async with StateStore(db_path) as store:
        await asyncio.gather(
            *(store.set_last_message_id(chat_id=1, message_id=i) for i in range(50))
        )
        assert await store.get_last_message_id(chat_id=1) == 49


def test_close_can_be_called_directly(db_path: Path) -> None:
    store = StateStore(db_path)
    store.close()
