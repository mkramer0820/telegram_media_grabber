"""Uploader worker: scans configured directories and pushes files to Telegram.

This module has no dependency on `src.ui` (dependency direction rule in
CLAUDE.md Section 1.3): progress is reported through the plain
`UploadProgressReporter` callback protocol, typically wired to
`src.ui.upload_dashboard` in `main.py`.

Multi-job routing: `UploaderWorker` is configured with one or more
`UploadJobConfig` entries (`src.config.settings`), each mapping a local
`source_dir` to a `target_chat`, with an optional `recursive` scan.

API shielding: files are batched into Telegram media groups (albums) of up
to `MEDIA_GROUP_MAX_SIZE` (Telegram's hard limit) via
`src.core.client.upload_media_group`, one request per batch instead of one
per file, with an explicit pause between batches (`_INTER_BATCH_DELAY_SECONDS`)
to proactively avoid tripping FloodWait. A batch never spans two jobs/target
chats — a media group is a single message to a single chat.

Deduplication: each file is checked against `StateStore.is_file_uploaded`
(keyed by `src.uploader.dedup.compute_dedup_key`, scoped to its target chat)
before upload, and recorded via `StateStore.mark_file_uploaded` immediately
after its batch sends successfully — mirroring the downloader's dedup/state
pattern in `src/downloader/worker.py`.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from telethon import TelegramClient

from src.config.settings import UploadJobConfig
from src.core.client import MEDIA_GROUP_MAX_SIZE, upload_media_group
from src.storage.state import StateStore
from src.uploader.dedup import compute_dedup_key

logger = logging.getLogger(__name__)

# Explicit pause between media-group batches, proactively spacing out
# requests rather than only reacting to FloodWaitError after the fact.
_INTER_BATCH_DELAY_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class UploadFileProgress:
    """A single progress data point for one in-flight upload batch."""

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
        """Called repeatedly as bytes are streamed for one in-flight batch."""

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


@dataclass(frozen=True, slots=True)
class _QueueItem:
    """One file queued for upload, paired with the chat it's destined for."""

    file_path: Path
    target_chat: int | str


class UploaderWorker:
    """Scans configured directories and uploads their files to target chats."""

    def __init__(
        self,
        client: TelegramClient,
        upload_jobs: list[UploadJobConfig],
        state_store: StateStore,
        reporter: UploadProgressReporter | None = None,
    ) -> None:
        """Initialize the worker.

        Args:
            client: A connected, authenticated `TelegramClient`.
            upload_jobs: One or more source-directory-to-target-chat jobs to
                process, as declared in `config/channels.yaml`.
            state_store: Shared state store used to skip files already
                uploaded to a given target chat and to record new ones.
            reporter: Optional progress callback surface (e.g. the `rich`
                upload dashboard). Defaults to a no-op implementation.
        """
        self._client = client
        self._upload_jobs = upload_jobs
        self._state_store = state_store
        self._reporter: UploadProgressReporter = reporter or _NullUploadProgressReporter()
        self._queue: list[_QueueItem] = []

    def build_queue(self) -> list[_QueueItem]:
        """Scan every configured job's `source_dir` and populate the upload queue.

        Each job is scanned recursively (via `Path.rglob`) or non-recursively
        (top-level files only, via `Path.iterdir`) per its `recursive` flag.
        A missing `source_dir` contributes no items for that job rather than
        raising, since "nothing to upload yet" is a normal state, not an
        error. Items are grouped by job in declaration order, sorted by path
        within each job.

        Returns:
            The list of queue items (file path + target chat) to upload.
        """
        queue: list[_QueueItem] = []
        for job in self._upload_jobs:
            if not job.source_dir.exists():
                continue
            candidates = job.source_dir.rglob("*") if job.recursive else job.source_dir.iterdir()
            files = sorted(path for path in candidates if path.is_file())
            queue.extend(_QueueItem(path, job.target_chat) for path in files)

        self._queue = queue
        return queue

    async def process_queue(self) -> None:
        """Upload every queued file, batched into per-chat media groups.

        Builds the queue first if `build_queue` hasn't been called yet. Each
        file is checked against `state_store` first; files already uploaded
        to their target chat are skipped rather than re-sent. Remaining
        files are grouped into batches of at most `MEDIA_GROUP_MAX_SIZE`
        files sharing the same target chat and sent as a single media-group
        request via `upload_media_group`, with `_INTER_BATCH_DELAY_SECONDS`
        between batches. A batch's failure is reported via `on_file_error`
        for each of its files and does not stop the remaining queue from
        being processed.
        """
        if not self._queue:
            self.build_queue()

        total = len(self._queue)
        uploaded = 0
        skipped = 0

        pending: list[tuple[_QueueItem, str]] = []
        for item in self._queue:
            dedup_key = compute_dedup_key(item.file_path)
            target_chat = str(item.target_chat)

            if await self._state_store.is_file_uploaded(target_chat, dedup_key):
                logger.info("Skipping already-uploaded file: %s", item.file_path)
                skipped += 1
                self._reporter.on_file_skipped(item.file_path.name)
                self._reporter.on_queue_progress(
                    UploadQueueProgress(total, uploaded, skipped, done=False)
                )
            else:
                pending.append((item, dedup_key))

        # Batches never span two target chats: group contiguous same-chat
        # runs first (the queue is already job-contiguous from build_queue),
        # then chunk each run to Telegram's per-album limit.
        batches: list[list[tuple[_QueueItem, str]]] = []
        for _, group_iter in itertools.groupby(pending, key=lambda pair: str(pair[0].target_chat)):
            group = list(group_iter)
            for start in range(0, len(group), MEDIA_GROUP_MAX_SIZE):
                batches.append(group[start : start + MEDIA_GROUP_MAX_SIZE])

        for batch_index, batch in enumerate(batches):
            target_chat_raw = batch[0][0].target_chat
            target_chat = str(target_chat_raw)
            file_paths = [item.file_path for item, _ in batch]

            try:

                def _progress(
                    current: int, total_bytes: int, _names: str = ", ".join(p.name for p in file_paths)
                ) -> None:
                    self._reporter.on_file_progress(
                        UploadFileProgress(_names, current, total_bytes)
                    )

                await upload_media_group(
                    self._client, target_chat_raw, file_paths, progress_callback=_progress
                )
            except Exception as exc:  # noqa: BLE001 - boundary: report & continue
                logger.exception("Permanent failure uploading batch to %s: %s", target_chat, file_paths)
                for item, _ in batch:
                    self._reporter.on_file_error(item.file_path.name, str(exc))
            else:
                for item, dedup_key in batch:
                    await self._state_store.mark_file_uploaded(target_chat, dedup_key, item.file_path)
                    uploaded += 1
                    self._reporter.on_file_complete(item.file_path.name)

            self._reporter.on_queue_progress(
                UploadQueueProgress(total, uploaded, skipped, done=False)
            )

            if batch_index < len(batches) - 1:
                await asyncio.sleep(_INTER_BATCH_DELAY_SECONDS)

        self._reporter.on_queue_progress(UploadQueueProgress(total, uploaded, skipped, done=True))
