"""SQLite-backed state tracking for scanned chats and downloaded files.

Per CLAUDE.md Section 4.3, all writes go through a single `StateStore`
instance that serializes access with an `asyncio.Lock`, so concurrent
downloader workers never issue overlapping writes to the same connection.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_progress (
    chat_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloaded_files (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT,
    downloaded_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_downloaded_files_hash
    ON downloaded_files (content_hash);
"""


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Thread-/task-safe wrapper around the state SQLite database.

    All public methods are coroutines that acquire an internal
    `asyncio.Lock` before touching the underlying `sqlite3.Connection`,
    satisfying the "no shared connection across concurrent writers without
    serialization" rule in CLAUDE.md.
    """

    def __init__(self, db_path: Path) -> None:
        """Open (creating if needed) the SQLite database at `db_path`.

        Args:
            db_path: Filesystem path to the state database file.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def get_last_message_id(self, chat_id: int) -> int | None:
        """Return the last fully-processed message ID for `chat_id`, if any."""
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT last_message_id FROM chat_progress WHERE chat_id = ?",
                (chat_id,),
            )
            row = cursor.fetchone()
            return int(row[0]) if row is not None else None

    async def set_last_message_id(self, chat_id: int, message_id: int) -> None:
        """Upsert the last-processed message ID for `chat_id`.

        Args:
            chat_id: Telegram chat/channel ID.
            message_id: Highest message ID fully processed so far.
        """
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO chat_progress (chat_id, last_message_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    updated_at = excluded.updated_at
                WHERE excluded.last_message_id > chat_progress.last_message_id
                """,
                (chat_id, message_id, _utc_now_iso()),
            )
            self._conn.commit()

    async def is_downloaded(self, chat_id: int, message_id: int) -> bool:
        """Return True if `(chat_id, message_id)` has already been recorded."""
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM downloaded_files WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            return cursor.fetchone() is not None

    async def record_downloaded_file(
        self,
        chat_id: int,
        message_id: int,
        file_path: Path,
        content_hash: str | None = None,
    ) -> None:
        """Record a completed download.

        Callers MUST only invoke this after the file has been atomically
        renamed into its final location (CLAUDE.md Section 2.5) — never
        before, and never for a `.part`/`.tmp` path.

        Args:
            chat_id: Telegram chat/channel ID the message belongs to.
            message_id: Source message ID.
            file_path: Final on-disk path of the downloaded file.
            content_hash: Optional content hash for cross-message dedup.
        """
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloaded_files
                    (chat_id, message_id, file_path, content_hash, downloaded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO NOTHING
                """,
                (chat_id, message_id, str(file_path), content_hash, _utc_now_iso()),
            )
            self._conn.commit()

    async def find_by_content_hash(self, content_hash: str) -> list[Path]:
        """Return file paths already stored under `content_hash`, if any."""
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT file_path FROM downloaded_files WHERE content_hash = ?",
                (content_hash,),
            )
            return [Path(row[0]) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    async def __aenter__(self) -> "StateStore":
        """Support `async with StateStore(...) as store:` usage."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection on context-manager exit."""
        self.close()
