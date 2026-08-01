"""Tests for src.uploader.dedup.compute_dedup_key."""

from __future__ import annotations

from pathlib import Path

from src.uploader.dedup import compute_dedup_key


def test_compute_dedup_key_stable_for_same_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"identical content")

    assert compute_dedup_key(path) == compute_dedup_key(path)


def test_compute_dedup_key_differs_for_different_content(tmp_path: Path) -> None:
    path_a = tmp_path / "a.txt"
    path_b = tmp_path / "b.txt"
    path_a.write_bytes(b"content one")
    path_b.write_bytes(b"content two, different length")

    assert compute_dedup_key(path_a) != compute_dedup_key(path_b)


def test_compute_dedup_key_differs_for_different_name_same_content(tmp_path: Path) -> None:
    path_a = tmp_path / "a.txt"
    path_b = tmp_path / "b.txt"
    path_a.write_bytes(b"same bytes")
    path_b.write_bytes(b"same bytes")

    assert compute_dedup_key(path_a) != compute_dedup_key(path_b)


def test_compute_dedup_key_differs_with_different_size_same_prefix(tmp_path: Path) -> None:
    path_a = tmp_path / "a.txt"
    path_b = tmp_path / "same_prefix" / "a.txt"
    path_b.parent.mkdir()

    path_a.write_bytes(b"X" * 10 + b"short")
    path_b.write_bytes(b"X" * 10 + b"a-much-longer-tail")

    # Different total size -> different key even with an identical hashed
    # prefix, since size is part of the key alongside the prefix hash.
    assert compute_dedup_key(path_a, prefix_bytes=10) != compute_dedup_key(
        path_b, prefix_bytes=10
    )


def test_compute_dedup_key_same_when_only_tail_differs_and_size_matches(
    tmp_path: Path,
) -> None:
    path_a = tmp_path / "a.txt"
    path_b = tmp_path / "sub" / "a.txt"
    path_b.parent.mkdir()

    path_a.write_bytes(b"X" * 10 + b"tailAAAAAA")
    path_b.write_bytes(b"X" * 10 + b"tailBBBBBB")

    # Same name, same size, same hashed prefix (first 10 bytes) -> same key,
    # even though the bytes beyond prefix_bytes differ (expected trade-off
    # of a fast prefix hash, documented in compute_dedup_key's docstring).
    assert compute_dedup_key(path_a, prefix_bytes=10) == compute_dedup_key(
        path_b, prefix_bytes=10
    )
