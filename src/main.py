"""Entry point: wires config, state, core, downloader, and UI layers together.

This module is the only place permitted to own the asyncio event loop
(CLAUDE.md Section 4.2) and the only place permitted to construct the
`Settings` object (CLAUDE.md Section 1.5).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from rich.console import Console
from telethon import TelegramClient

from src.config.settings import Settings, get_settings
from src.core.client import build_client, connect_and_authenticate
from src.core.exceptions import DownloaderError
from src.downloader.worker import DownloadManager
from src.storage.state import StateStore
from src.ui.dashboard import Dashboard
from src.ui.logging_config import configure_logging
from src.ui.upload_dashboard import UploadDashboard
from src.uploader.worker import UploaderWorker

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments selecting the run mode.

    Returns:
        Parsed arguments; `mode` is either `"download"` (default) or
        `"upload"`.
    """
    parser = argparse.ArgumentParser(description="Telegram Batch Media Downloader/Uploader")
    parser.add_argument(
        "--mode",
        choices=["download", "upload"],
        default="download",
        help="Run in 'download' mode (default) or 'upload' mode.",
    )
    return parser.parse_args()


async def _run_download(settings: Settings, console: Console, client: TelegramClient) -> None:
    """Scan every configured channel and download new media.

    Args:
        settings: Fully-loaded application settings (env + channels.yaml).
        console: Shared `rich` console used for all terminal output.
        client: A connected, authenticated `TelegramClient`.
    """
    console.print(f"Tracking [bold]{len(settings.channels_file.channels)}[/bold] channel(s).")

    async with StateStore(settings.state_db_path) as state_store:
        with Dashboard(console) as dashboard:
            manager = DownloadManager(
                client=client,
                state_store=state_store,
                download_root=settings.channels_file.download_root,
                max_concurrent_downloads=settings.channels_file.max_concurrent_downloads,
                audiobooks_dest_dir=settings.audiobooks_dest_dir,
                reporter=dashboard,
            )
            await manager.run(settings.channels_file.channels)


async def _run_upload(settings: Settings, console: Console, client: TelegramClient) -> None:
    """Run every configured upload job, sending each job's files to its target chat.

    Args:
        settings: Fully-loaded application settings (env + channels.yaml).
        console: Shared `rich` console used for all terminal output.
        client: A connected, authenticated `TelegramClient`.

    Raises:
        DownloaderError: If `upload_jobs` is empty in `config/channels.yaml`.
    """
    upload_jobs = settings.channels_file.upload_jobs
    if not upload_jobs:
        raise DownloaderError(
            "No upload_jobs configured in config/channels.yaml; "
            "upload mode has nothing to send."
        )

    console.print(f"Running [bold]{len(upload_jobs)}[/bold] upload job(s).")

    async with StateStore(settings.state_db_path) as state_store:
        with UploadDashboard(console) as dashboard:
            worker = UploaderWorker(
                client=client,
                upload_jobs=upload_jobs,
                state_store=state_store,
                reporter=dashboard,
            )
            await worker.process_queue()


async def run(settings: Settings, console: Console, mode: str) -> None:
    """Run the application's main async workflow.

    Args:
        settings: Fully-loaded application settings (env + channels.yaml).
        console: Shared `rich` console used for all terminal output.
        mode: Either `"download"` or `"upload"`, selecting which workflow
            runs against the shared, already-authenticated client.
    """
    console.print("[bold cyan]Telegram Batch Media Downloader/Uploader[/bold cyan]")

    client = build_client(settings)
    await connect_and_authenticate(client, settings, console)

    try:
        if mode == "upload":
            await _run_upload(settings, console, client)
        else:
            await _run_download(settings, console, client)
    finally:
        await client.disconnect()

    console.print("[bold green]Done.[/bold green]")


def main() -> None:
    """Synchronous process entry point (`python -m src.main`)."""
    args = _parse_args()
    settings = get_settings()
    configure_logging(settings.log_file_path)
    console = Console()

    try:
        asyncio.run(run(settings, console, args.mode))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user, shutting down.[/yellow]")


if __name__ == "__main__":
    main()
