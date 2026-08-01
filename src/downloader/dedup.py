"""Deduplication key computation (CLAUDE.md Section 2.4).

Two layers of dedup are supported:
  1. Identity dedup: `(chat_id, message_id)` — enforced by the SQLite
     `UNIQUE` constraint in `src.storage.state`, checked before download.
  2. Content dedup: an optional hash of the downloaded bytes, used to
     detect the same media reposted under a different message.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def message_dedup_key(chat_id: int, message_id: int) -> tuple[int, int]:
    """Return the identity dedup key for a Telegram message.

    Args:
        chat_id: Telegram chat/channel ID.
        message_id: Message ID within that chat.

    Returns:
        A `(chat_id, message_id)` tuple matching the SQLite primary key.
    """
    return (chat_id, message_id)


def hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute a SHA-256 hex digest of the file at `path`.

    Reads in fixed-size chunks so large media files do not need to be
    fully loaded into memory.

    Args:
        path: Path to a fully-written, closed file on disk.
        chunk_size: Number of bytes to read per iteration.

    Returns:
        The hex-encoded SHA-256 digest of the file's contents.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
