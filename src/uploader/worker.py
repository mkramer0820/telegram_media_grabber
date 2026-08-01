"""Uploader worker: scans a local directory and pushes files to a Telegram chat.

This module has no dependency on `src.ui` (dependency direction rule in
CLAUDE.md Section 1.3): progress is reported through the plain
`UploadProgressReporter` callback protocol, typically wired to
`src.ui.upload_dashboard` in `main.py`.

Concurrency: the queue is processed strictly one file at a time. Telegram
upload throughput benefits far less from parallel transfer than downloads
do, and sequential processing keeps `upload_document`'s FloodWait/backoff
policy simple to reason about (CLAUDE.md Section 4.4's bounded-concurrency
requirement is trivially satisfied at concurrency=1).

Deduplication: each file is checked against `StateStore.is_file_uploaded`
(keyed by `src.uploader.dedup.compute_dedup_key`, scoped to the target chat)
before upload, and recorded via `StateStore.mark_file_uploaded` immediately
after a successful send — mirroring the downloader's dedup/state pattern in
`src/downloader/worker.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from telethon import TelegramClient

from src.core.client import upload_document
from src.storage.state import StateStore
from src.uploader.dedup import compute_dedup_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadFileProgress:
    """A single progress data point for one in-flight file upload."""

    filename: str
    bytes_done: int
    bytes_total: int


@dataclass(frozen=True, slots=True)
class UploadQueueProgress:
    """Reports overall progress through the upload queue."""

    files_total: int
    files_uploaded: int
    files_skipped: int
    done: bool


class UploadProgressReporter(Protocol):
    """Callback surface the UI layer implements to observe upload progress.

    All methods are synchronous and MUST return quickly (e.g. update
    in-memory state for a `rich.Live` display) — they run on the event loop
    between upload chunks.
    """

    def on_file_progress(self, progress: UploadFileProgress) -> None:
        """Called repeatedly as bytes are streamed for one file."""

    def on_file_complete(self, filename: str) -> None:
        """Called once a file has finished uploading successfully."""

    def on_file_error(self, filename: str, error: str) -> None:
        """Called when a file permanently fails after exhausting retries."""

    def on_file_skipped(self, filename: str) -> None:
        """Called when a file is skipped because it was already uploaded."""

    def on_queue_progress(self, progress: UploadQueueProgress) -> None:
        """Called as files are processed from the queue."""


class _NullUploadProgressReporter:
    """No-op reporter used when the caller doesn't supply one."""

    def on_file_progress(self, progress: UploadFileProgress) -> None:
        """Discard the update."""

    def on_file_complete(self, filename: str) -> None:
        """Discard the update."""

    def on_file_error(self, filename: str, error: str) -> None:
        """Discard the update."""

    def on_file_skipped(self, filename: str) -> None:
        """Discard the update."""

    def on_queue_progress(self, progress: UploadQueueProgress) -> None:
        """Discard the update."""


class UploaderWorker:
    """Scans a local directory and uploads its files to a target Telegram chat."""

    def __init__(
        self,
        client: TelegramClient,
        target_chat: int | str,
        source_directory: Path,
        state_store: StateStore,
        reporter: UploadProgressReporter | None = None,
    ) -> None:
        """Initialize the worker.

        Args:
            client: A connected, authenticated `TelegramClient`.
            target_chat: Destination chat ID, "@username", or invite link,
                as accepted by `src.core.client.resolve_entity`.
            source_directory: Local directory scanned for files to upload.
            state_store: Shared state store used to skip files already
                uploaded to `target_chat` and to record newly uploaded ones.
            reporter: Optional progress callback surface (e.g. the `rich`
                upload dashboard). Defaults to a no-op implementation.
        """
        self._client = client
        self._target_chat = target_chat
        self._source_directory = source_directory
        self._state_store = state_store
        self._reporter: UploadProgressReporter = reporter or _NullUploadProgressReporter()
        self._queue: list[Path] = []

    def build_queue(self) -> list[Path]:
        """Scan `source_directory` for files and populate the upload queue.

        The scan is non-recursive (top-level files only) and results are
        sorted by name for deterministic, resumable-by-eye ordering. A
        missing source directory yields an empty queue rather than raising,
        since "nothing to upload yet" is a normal state, not an error.

        Returns:
            The list of file paths queued for upload.
        """
        if not self._source_directory.exists():
            self._queue = []
            return self._queue

        self._queue = sorted(
            path for path in self._source_directory.iterdir() if path.is_file()
        )
        return self._queue

    async def process_queue(self) -> None:
        """Upload every queued file in order, reporting progress as it goes.

        Builds the queue first if `build_queue` hasn't been called yet. Each
        file is checked against `state_store` first; files already uploaded
        to `target_chat` are skipped rather than re-sent. A single file's
        failure is reported via `on_file_error` and does not stop the
        remaining queue from being processed.
        """
        if not self._queue:
            self.build_queue()

        target_chat = str(self._target_chat)
        total = len(self._queue)
        uploaded = 0
        skipped = 0

        for file_path in self._queue:
            dedup_key = compute_dedup_key(file_path)

            if await self._state_store.is_file_uploaded(target_chat, dedup_key):
                logger.info("Skipping already-uploaded file: %s", file_path)
                skipped += 1
                self._reporter.on_file_skipped(file_path.name)
                self._reporter.on_queue_progress(
                    UploadQueueProgress(total, uploaded, skipped, done=False)
                )
                continue

            try:

                def _progress(current: int, total_bytes: int, _name: str = file_path.name) -> None:
                    self._reporter.on_file_progress(
                        UploadFileProgress(_name, current, total_bytes)
                    )

                await upload_document(
                    self._client,
                    self._target_chat,
                    file_path,
                    progress_callback=_progress,
                )
            except Exception as exc:  # noqa: BLE001 - boundary: report & continue
                logger.exception("Permanent failure uploading %s", file_path)
                self._reporter.on_file_error(file_path.name, str(exc))
            else:
                await self._state_store.mark_file_uploaded(target_chat, dedup_key, file_path)
                uploaded += 1
                self._reporter.on_file_complete(file_path.name)

            self._reporter.on_queue_progress(
                UploadQueueProgress(total, uploaded, skipped, done=False)
            )

        self._reporter.on_queue_progress(UploadQueueProgress(total, uploaded, skipped, done=True))
