"""Re-verifies `audiobook_mode` episode numbers against Telegram: `--mode verify`.

Before `extract_episode_info` supported bare-numeric filenames (see
`src.downloader.audiobook_processor`), a chapter whose raw filename was
just a number (e.g. "1053.m4a", no "Ep n" text) fell back to Telegram's
message ID as its episode number — an arbitrary, unrelated number. Files
tagged that way already exist on disk with the wrong number baked into
both their filename and ID3/MP4 tags.

This module re-fetches each already-recorded file's source message
directly from Telegram, re-derives the true episode number from its raw
document filename using the fixed `extract_episode_info`, and — only when
that differs from what's currently on disk — re-tags and relocates the
file, then corrects its `downloaded_files` row.

Online: makes one batched `get_messages` request per channel. Read-only
against Telegram; only local files and state are modified.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from telethon import TelegramClient
from telethon.tl.custom.message import Message

from src.config.settings import ChannelConfig
from src.core.client import resolve_entity
from src.downloader.audiobook_processor import (
    apply_episode_tagging,
    extract_episode_info,
    parse_tagged_episode_number,
)
from src.downloader.dedup import hash_file
from src.downloader.worker import derive_filename
from src.storage.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VerifySummary:
    """Outcome of one `EpisodeVerifier.run_channel` call."""

    checked: int
    corrected: int
    errors: int

    def __add__(self, other: "VerifySummary") -> "VerifySummary":
        """Combine two summaries (used to total across multiple channels)."""
        return VerifySummary(
            checked=self.checked + other.checked,
            corrected=self.corrected + other.corrected,
            errors=self.errors + other.errors,
        )


class EpisodeVerifier:
    """Cross-checks and corrects `audiobook_mode` episode numbers against Telegram."""

    def __init__(
        self, client: TelegramClient, state_store: StateStore, audiobooks_dest_dir: Path
    ) -> None:
        """Initialize the verifier.

        Args:
            client: A connected, authenticated `TelegramClient`.
            state_store: Shared state store, used to enumerate a channel's
                recorded files and to correct any that get relocated.
            audiobooks_dest_dir: Destination root for tagged audiobooks
                (only used when a correction actually relocates a file).
        """
        self._client = client
        self._state_store = state_store
        self._audiobooks_dest_dir = audiobooks_dest_dir

    async def run_channel(
        self, channel: ChannelConfig, console: Console | None = None
    ) -> VerifySummary:
        """Verify every recorded file for `channel` against Telegram.

        Args:
            channel: An `audiobook_mode` channel with `metadata` set (the
                caller is responsible for filtering to such channels).
            console: Optional `rich` console for a one-line-per-correction report.

        Returns:
            A summary of how many files were checked, corrected, or errored.
        """
        assert channel.metadata is not None, "caller must filter to metadata-having channels"

        entity = await resolve_entity(self._client, channel.id)
        chat_id = int(entity.id)

        records = await self._state_store.list_downloaded_records(chat_id)
        if not records:
            return VerifySummary(checked=0, corrected=0, errors=0)

        message_ids = [message_id for message_id, _ in records]
        messages = await self._client.get_messages(entity, ids=message_ids)

        checked = corrected = errors = 0
        for (message_id, file_path), message in zip(records, messages):
            if message is None or not file_path.exists():
                continue
            checked += 1
            try:
                fixed_path = await self._verify_one(channel, chat_id, message_id, file_path, message)
            except Exception as exc:  # noqa: BLE001 - boundary: report & continue
                logger.exception("Failed to verify episode number for %s", file_path)
                if console is not None:
                    console.print(f"[red]Error[/red] {file_path.name}: {exc}")
                errors += 1
                continue

            if fixed_path is not None:
                corrected += 1
                if console is not None:
                    console.print(f"[green]Corrected[/green] {file_path.name} -> {fixed_path.name}")

        return VerifySummary(checked=checked, corrected=corrected, errors=errors)

    async def _verify_one(
        self, channel: ChannelConfig, chat_id: int, message_id: int, file_path: Path, message: Message
    ) -> Path | None:
        """Correct one file's episode number if Telegram's truth disagrees.

        Args:
            channel: The file's owning channel (must have `metadata` set).
            chat_id: Numeric Telegram chat ID (already resolved by the caller).
            message_id: The file's source message ID.
            file_path: The file's current on-disk location.
            message: The freshly-fetched source message.

        Returns:
            The file's new path if a correction was applied, or `None` if
            the current episode number already matches Telegram's truth (or
            Telegram's raw filename carries no parsable number of its own,
            in which case there's nothing more trustworthy to correct to).
        """
        assert channel.metadata is not None

        true_info = extract_episode_info(derive_filename(message))
        if true_info is None:
            return None

        current_episode = parse_tagged_episode_number(file_path)
        if current_episode == true_info.episode:
            return None

        new_path = await apply_episode_tagging(
            file_path, true_info, channel.metadata, self._audiobooks_dest_dir
        )
        content_hash = await asyncio.to_thread(hash_file, new_path)
        await self._state_store.update_downloaded_file_path(chat_id, message_id, new_path, content_hash)
        logger.info(
            "Corrected episode number for message_id=%d: %s (was Ep %s) -> %s (Ep %d)",
            message_id, file_path, current_episode, new_path, true_info.episode,
        )
        return new_path
