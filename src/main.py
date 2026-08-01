"""Entry point: wires config, state, core, downloader, and UI layers together.

This module is the only place permitted to own the asyncio event loop
(CLAUDE.md Section 4.2) and the only place permitted to construct the
`Settings` object (CLAUDE.md Section 1.5).
"""

from __future__ import annotations

import asyncio
import logging

from rich.console import Console

from src.config.settings import Settings, get_settings
from src.core.client import build_client, connect_and_authenticate
from src.downloader.worker import DownloadManager
from src.storage.state import StateStore
from src.ui.dashboard import Dashboard
from src.ui.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def run(settings: Settings, console: Console) -> None:
    """Run the application's main async workflow.

    Args:
        settings: Fully-loaded application settings (env + channels.yaml).
        console: Shared `rich` console used for all terminal output.
    """
    console.print("[bold cyan]Telegram Batch Media Downloader[/bold cyan]")
    console.print(f"Tracking [bold]{len(settings.channels_file.channels)}[/bold] channel(s).")

    client = build_client(settings)
    await connect_and_authenticate(client, settings, console)

    try:
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
    finally:
        await client.disconnect()

    console.print("[bold green]Done.[/bold green]")


def main() -> None:
    """Synchronous process entry point (`python -m src.main`)."""
    settings = get_settings()
    configure_logging(settings.log_file_path)
    console = Console()

    try:
        asyncio.run(run(settings, console))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user, shutting down.[/yellow]")


if __name__ == "__main__":
    main()
