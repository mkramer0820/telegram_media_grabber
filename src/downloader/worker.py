"""Async, semaphore-bounded download workers.

Implements:
  - Atomic writes: media is streamed to a `.tmp` path and only renamed to
    its final, sanitized name via `os.replace` once fully written
    (CLAUDE.md Section 2.1).
  - Bounded concurrency via `asyncio.Semaphore` (CLAUDE.md Section 4.4).
  - `FloodWaitError` handling: the exact server-specified wait is always
    honored (CLAUDE.md Section 4.6); an additional capped exponential
    backoff is layered on top for *repeated* flood waits on the same task,
    and for other transient network errors.
  - Clean cancellation: on `asyncio.CancelledError`, in-flight `.tmp` files
    are left in place (resumable) rather than renamed (CLAUDE.md Section 4.5).

This module has no dependency on `src.ui` (dependency direction rule in
CLAUDE.md Section 1.3): progress is reported through plain callables passed
in by the caller (typically wired to `src.ui.dashboard` in `main.py`).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Protocol

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.custom.message import Message

from src.config.settings import ChannelConfig, MediaType
from src.downloader.dedup import hash_file
from src.downloader.filenames import dedup_suffixed_path, sanitize_filename
from src.storage.state import StateStore

logger = logging.getLogger(__name__)

_MAX_TRANSIENT_RETRIES = 5
_BASE_BACKOFF_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class FileProgress:
    """A single progress data point for one in-flight file download."""

    chat_name: str
    message_id: int
    filename: str
    bytes_done: int
    bytes_total: int


@dataclass(frozen=True, slots=True)
class ChannelProgress:
    """Reports overall scan progress for one channel."""

    chat_name: str
    messages_scanned: int
    files_downloaded: int
    done: bool


class ProgressReporter(Protocol):
    """Callback surface the UI layer implements to observe download progress.

    All methods are synchronous and MUST return quickly (e.g. update
    in-memory state for a `rich.Live` display) — they run on the event loop
    between download chunks.
    """

    def on_file_progress(self, progress: FileProgress) -> None:
        """Called repeatedly as bytes are streamed for one file."""

    def on_file_complete(self, chat_name: str, message_id: int, final_path: Path) -> None:
        """Called once a file has been atomically renamed into place."""

    def on_file_error(self, chat_name: str, message_id: int, error: str) -> None:
        """Called when a file permanently fails after exhausting retries."""

    def on_channel_progress(self, progress: ChannelProgress) -> None:
        """Called as messages are scanned within a channel."""

    def on_flood_wait(self, seconds: float) -> None:
        """Called when the worker pool is pausing for a FloodWaitError."""


class _NullProgressReporter:
    """No-op reporter used when the caller doesn't supply one."""

    def on_file_progress(self, progress: FileProgress) -> None:
        """Discard the update."""

    def on_file_complete(self, chat_name: str, message_id: int, final_path: Path) -> None:
        """Discard the update."""

    def on_file_error(self, chat_name: str, message_id: int, error: str) -> None:
        """Discard the update."""

    def on_channel_progress(self, progress: ChannelProgress) -> None:
        """Discard the update."""

    def on_flood_wait(self, seconds: float) -> None:
        """Discard the update."""


_MEDIA_TYPE_ATTR = {
    MediaType.PHOTO: "photo",
    MediaType.VIDEO: "video",
    MediaType.DOCUMENT: "document",
    MediaType.AUDIO: "audio",
}


def _matches_media_types(message: Message, wanted: list[MediaType]) -> bool:
    """Return True if `message` carries one of the media kinds in `wanted`."""
    for media_type in wanted:
        attr = _MEDIA_TYPE_ATTR[media_type]
        if getattr(message, attr, None) is not None:
            return True
    return False


def _derive_filename(message: Message) -> str:
    """Best-effort filename for a message's media, before sanitization."""
    if message.document is not None:
        for attribute in message.document.attributes:
            name = getattr(attribute, "file_name", None)
            if name:
                return str(name)
    return f"{message.chat_id}_{message.id}"


class DownloadManager:
    """Coordinates bounded-concurrency downloads for a set of channels."""

    def __init__(
        self,
        client: TelegramClient,
        state_store: StateStore,
        download_root: Path,
        max_concurrent_downloads: int,
        reporter: ProgressReporter | None = None,
    ) -> None:
        """Initialize the manager.

        Args:
            client: A connected, authenticated `TelegramClient`.
            state_store: Shared state store for resume/dedup tracking.
            download_root: Base directory under which channel subdirs live.
            max_concurrent_downloads: Upper bound on simultaneous file
                downloads across all channels (CLAUDE.md Section 4.4).
            reporter: Optional progress callback surface (e.g. the `rich`
                dashboard). Defaults to a no-op implementation.
        """
        self._client = client
        self._state_store = state_store
        self._download_root = download_root
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads)
        self._reporter: ProgressReporter = reporter or _NullProgressReporter()

    async def run(self, channels: list[ChannelConfig]) -> None:
        """Process every channel concurrently, respecting the shared semaphore.

        Args:
            channels: Channels declared in `config/channels.yaml`.
        """
        await asyncio.gather(*(self._process_channel(channel) for channel in channels))

    async def _process_channel(self, channel: ChannelConfig) -> None:
        """Scan one channel's history and dispatch bounded download tasks.

        Args:
            channel: The channel/chat configuration to process.
        """
        entity = await self._client.get_entity(channel.id)
        chat_id = int(entity.id)
        last_message_id = await self._state_store.get_last_message_id(chat_id) or 0

        output_dir = self._download_root / channel.output_subdir
        output_dir.mkdir(parents=True, exist_ok=True)

        scanned = 0
        downloaded = 0
        tasks: list[Awaitable[None]] = []
        highest_seen = last_message_id

        async for message in self._client.iter_messages(entity, min_id=last_message_id):
            scanned += 1
            highest_seen = max(highest_seen, message.id)

            if not _matches_media_types(message, channel.media_types):
                continue
            if await self._state_store.is_downloaded(chat_id, message.id):
                continue

            tasks.append(self._download_one(channel.name, chat_id, message, output_dir))
            downloaded += 1

            self._reporter.on_channel_progress(
                ChannelProgress(channel.name, scanned, downloaded, done=False)
            )

        if tasks:
            await asyncio.gather(*tasks)

        if highest_seen > last_message_id:
            await self._state_store.set_last_message_id(chat_id, highest_seen)

        self._reporter.on_channel_progress(
            ChannelProgress(channel.name, scanned, downloaded, done=True)
        )

    async def _download_one(
        self, chat_name: str, chat_id: int, message: Message, output_dir: Path
    ) -> None:
        """Download a single message's media under the shared semaphore.

        Args:
            chat_name: Human-readable channel label, for reporting.
            chat_id: Numeric Telegram chat ID.
            message: The message whose media should be downloaded.
            output_dir: Directory the final file should land in.
        """
        async with self._semaphore:
            raw_name = _derive_filename(message)
            safe_name = sanitize_filename(raw_name)
            final_path = dedup_suffixed_path(output_dir / safe_name)
            tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")

            try:
                await self._download_with_retries(chat_name, message, tmp_path)
                # Atomic rename: only now is the file considered "downloaded"
                # (CLAUDE.md Section 2.1 / 2.5).
                await asyncio.to_thread(tmp_path.replace, final_path)
            except asyncio.CancelledError:
                logger.warning(
                    "Download cancelled for chat=%s message=%s; leaving %s for resume.",
                    chat_id, message.id, tmp_path,
                )
                raise
            except Exception as exc:  # noqa: BLE001 - boundary: report & continue
                logger.exception(
                    "Permanent failure downloading chat=%s message=%s", chat_id, message.id
                )
                tmp_path.unlink(missing_ok=True)
                self._reporter.on_file_error(chat_name, message.id, str(exc))
                return

            content_hash = await asyncio.to_thread(hash_file, final_path)
            await self._state_store.record_downloaded_file(
                chat_id, message.id, final_path, content_hash
            )
            self._reporter.on_file_complete(chat_name, message.id, final_path)

    async def _download_with_retries(
        self, chat_name: str, message: Message, tmp_path: Path
    ) -> None:
        """Download `message`'s media to `tmp_path`, retrying transient errors.

        `FloodWaitError` always sleeps for at least the server-specified
        duration. Repeated flood waits (and other transient network errors)
        additionally back off exponentially, capped at `_MAX_BACKOFF_SECONDS`,
        up to `_MAX_TRANSIENT_RETRIES` attempts.

        Args:
            chat_name: Human-readable channel label, for progress reporting.
            message: The message being downloaded.
            tmp_path: Temporary `.tmp` destination path.

        Raises:
            Exception: Re-raises the last error once retries are exhausted.
        """
        attempt = 0
        while True:
            try:

                def _progress(current: int, total: int) -> None:
                    self._reporter.on_file_progress(
                        FileProgress(chat_name, message.id, tmp_path.name, current, total)
                    )

                await message.download_media(file=str(tmp_path), progress_callback=_progress)
                return
            except FloodWaitError as exc:
                attempt += 1
                backoff = min(_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)
                wait_seconds = float(exc.seconds) + backoff
                logger.warning(
                    "FloodWaitError on chat message=%s: sleeping %.1fs "
                    "(server=%ss + backoff=%.1fs, attempt %d)",
                    message.id, wait_seconds, exc.seconds, backoff, attempt,
                )
                self._reporter.on_flood_wait(wait_seconds)
                await asyncio.sleep(wait_seconds)
                if attempt >= _MAX_TRANSIENT_RETRIES:
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transient network/IO error
                attempt += 1
                if attempt >= _MAX_TRANSIENT_RETRIES:
                    raise
                backoff = min(
                    _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS
                ) + random.uniform(0, 1)
                logger.warning(
                    "Transient error downloading message=%s (attempt %d/%d): %s; "
                    "retrying in %.1fs",
                    message.id, attempt, _MAX_TRANSIENT_RETRIES, exc, backoff,
                )
                await asyncio.sleep(backoff)
