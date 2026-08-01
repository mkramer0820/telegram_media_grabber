"""Repairs `audiobook_mode` files stuck in staging: `--mode reprocess`.

A file can end up downloaded (recorded in `downloaded_files`) but never
tagged and relocated out of its staging directory — e.g. it was fetched
before `audiobook_mode`/`metadata` was configured for that channel, or
`process_audiobook_file` failed at the time. Because dedup is keyed on
`(chat_id, message_id)`, such a file is never retried by a normal download
run once it's marked downloaded, so it sits in staging indefinitely.

This module finds those files by scanning each `audiobook_mode` channel's
staging directory directly (a successfully-processed file is moved OUT of
it — anything still there needs reprocessing, by definition) and re-runs
tagging/relocation, then corrects the `downloaded_files` row's `file_path`.

Fully offline: works entirely from local files and `StateStore`. No
Telegram connection is made or needed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from src.config.settings import ChannelConfig
from src.downloader.audiobook_processor import process_audiobook_file
from src.downloader.dedup import hash_file
from src.storage.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReprocessSummary:
    """Outcome of one `AudiobookReprocessor.run` call."""

    processed: int
    skipped: int
    errors: int


def find_stuck_files(download_root: Path, channel: ChannelConfig) -> list[Path]:
    """List files still sitting in `channel`'s staging directory.

    Args:
        download_root: `Settings.channels_file.download_root`.
        channel: An `audiobook_mode` channel configuration.

    Returns:
        Sorted list of file paths still in `download_root/channel.output_subdir`,
        or an empty list if that directory doesn't exist.
    """
    staging_dir = download_root / channel.output_subdir
    if not staging_dir.exists():
        return []
    return sorted(path for path in staging_dir.iterdir() if path.is_file())


class AudiobookReprocessor:
    """Re-runs tagging/relocation for `audiobook_mode` files stuck in staging."""

    def __init__(
        self, state_store: StateStore, download_root: Path, audiobooks_dest_dir: Path
    ) -> None:
        """Initialize the reprocessor.

        Args:
            state_store: Shared state store, used to look up which
                `(chat_id, message_id)` a stuck file belongs to (by its
                current staging path) and to correct that row afterward.
            download_root: Base directory staging subdirectories live under.
            audiobooks_dest_dir: Destination root for tagged audiobooks.
        """
        self._state_store = state_store
        self._download_root = download_root
        self._audiobooks_dest_dir = audiobooks_dest_dir

    async def run(
        self, channels: list[ChannelConfig], console: Console | None = None
    ) -> ReprocessSummary:
        """Reprocess every stuck file across the given `audiobook_mode` channels.

        Args:
            channels: `audiobook_mode` channels to scan. Each must have
                `metadata` set (the caller is responsible for filtering —
                see `ChannelConfig`'s validator, which already guarantees
                this for any channel with `audiobook_mode: true`).
            console: Optional `rich` console for a one-line-per-file report.

        Returns:
            A summary of how many files were processed, skipped (no
            matching `downloaded_files` record found), or errored.
        """
        processed = skipped = errors = 0
        for channel in channels:
            for file_path in find_stuck_files(self._download_root, channel):
                try:
                    new_path = await self._reprocess_one(channel, file_path)
                except Exception as exc:  # noqa: BLE001 - boundary: report & continue
                    logger.exception("Failed to reprocess %s", file_path)
                    if console is not None:
                        console.print(f"[red]Error[/red] {file_path.name}: {exc}")
                    errors += 1
                    continue

                if new_path is None:
                    skipped += 1
                    if console is not None:
                        console.print(
                            f"[yellow]Skipped[/yellow] {file_path.name} "
                            "(no matching downloaded_files record)"
                        )
                else:
                    processed += 1
                    if console is not None:
                        console.print(f"[green]Reprocessed[/green] {file_path.name} -> {new_path.name}")

        return ReprocessSummary(processed=processed, skipped=skipped, errors=errors)

    async def _reprocess_one(self, channel: ChannelConfig, file_path: Path) -> Path | None:
        """Tag, move, and correct state for one stuck file.

        Args:
            channel: The file's owning channel (must have `metadata` set).
            file_path: Current on-disk location, still in staging.

        Returns:
            The file's new path if reprocessed, or `None` if no
            `downloaded_files` record matches `file_path` — left in place
            rather than guessed at.
        """
        assert channel.metadata is not None, "caller must filter to metadata-having channels"

        record = await self._state_store.find_downloaded_record_by_path(file_path)
        if record is None:
            return None
        chat_id, message_id = record

        new_path = await process_audiobook_file(
            file_path, message_id, channel.metadata, self._audiobooks_dest_dir
        )
        content_hash = await asyncio.to_thread(hash_file, new_path)
        await self._state_store.update_downloaded_file_path(chat_id, message_id, new_path, content_hash)
        return new_path
