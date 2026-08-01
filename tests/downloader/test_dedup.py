"""Tests for media dedup key / content hashing (CLAUDE.md Section 2.4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.downloader.dedup import hash_file, message_dedup_key


def test_message_dedup_key_returns_identity_tuple() -> None:
    assert message_dedup_key(chat_id=123, message_id=456) == (123, 456)


def test_hash_file_matches_known_sha256(tmp_path: Path) -> None:
    content = b"the quick brown fox jumps over the lazy dog"
    file_path = tmp_path / "sample.bin"
    file_path.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert hash_file(file_path) == expected


def test_hash_file_identical_content_same_hash(tmp_path: Path) -> None:
    content = b"duplicate media bytes" * 1000
    file_a = tmp_path / "a.mp4"
    file_b = tmp_path / "b.mp4"
    file_a.write_bytes(content)
    file_b.write_bytes(content)

    assert hash_file(file_a) == hash_file(file_b)


def test_hash_file_different_content_different_hash(tmp_path: Path) -> None:
    file_a = tmp_path / "a.mp4"
    file_b = tmp_path / "b.mp4"
    file_a.write_bytes(b"content one")
    file_b.write_bytes(b"content two")

    assert hash_file(file_a) != hash_file(file_b)


def test_hash_file_handles_content_larger_than_chunk_size(tmp_path: Path) -> None:
    content = b"x" * (2 * 1024 * 1024 + 17)  # spans multiple 1MB chunks
    file_path = tmp_path / "large.bin"
    file_path.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert hash_file(file_path, chunk_size=64 * 1024) == expected
