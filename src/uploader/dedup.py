"""Deduplication key computation for upload mode.

Mirrors `src.downloader.dedup`'s role but computes a key cheap enough to run
on every scan without re-reading whole files: filename + size + a hash of
only the first chunk, rather than the full content hash the downloader uses
after a file is already fully on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def compute_dedup_key(path: Path, *, prefix_bytes: int = 1024 * 1024) -> str:
    """Return a stable dedup key for `path`.

    Combines the filename, file size, and a SHA-256 hash of just the first
    `prefix_bytes` bytes. This is a fast approximation, not a full-content
    hash: two distinct files that happen to share a name, size, and first
    chunk would collide, but for local upload-directory scans that trade-off
    avoids hashing potentially large media files in full on every run.

    Args:
        path: Path to the local file to key.
        prefix_bytes: Number of leading bytes to hash.

    Returns:
        A `"{name}:{size}:{hex_digest}"` string suitable as a `StateStore`
        `dedup_key`.
    """
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(prefix_bytes))
    return f"{path.name}:{size}:{digest.hexdigest()}"
