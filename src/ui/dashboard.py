"""Live `rich` terminal dashboard: overall channel progress + per-file bars.

Implements `src.downloader.worker.ProgressReporter` so it can be handed
directly to `DownloadManager` without the downloader layer importing
anything from `ui` (CLAUDE.md Section 1.3 dependency direction). All output
here goes through `rich` — no `print()` (CLAUDE.md Section 3.1).
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from src.downloader.worker import ChannelProgress, FileProgress


class Dashboard:
    """A `Live` dashboard showing channel scan progress and active downloads.

    Usage:
        with Dashboard(console) as dashboard:
            manager = DownloadManager(..., reporter=dashboard)
            await manager.run(channels)
    """

    def __init__(self, console: Console) -> None:
        """Initialize the dashboard's `rich` components.

        Args:
            console: Shared console instance used for all rendering.
        """
        self._console = console

        self._file_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.fields[chat_name]}[/bold cyan] {task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        self._file_task_ids: dict[tuple[str, int], TaskID] = {}

        self._channel_rows: dict[str, ChannelProgress] = {}
        self._status_line = ""

        self._live = Live(self._render(), console=console, refresh_per_second=8, transient=False)

    def _render(self) -> Group:
        """Compose the current frame: channel summary table + file progress bars."""
        table = Table(title="Channels", expand=True)
        table.add_column("Channel")
        table.add_column("Scanned", justify="right")
        table.add_column("Downloaded", justify="right")
        table.add_column("Status", justify="right")

        for progress in self._channel_rows.values():
            status = "[green]done[/green]" if progress.done else "[yellow]scanning[/yellow]"
            table.add_row(
                progress.chat_name,
                str(progress.messages_scanned),
                str(progress.files_downloaded),
                status,
            )

        panels: list[Table | Panel | Progress] = [table, self._file_progress]
        if self._status_line:
            panels.append(Panel(self._status_line, style="yellow"))

        return Group(*panels)

    def _refresh(self) -> None:
        """Re-render and push the updated frame to the live display."""
        self._live.update(self._render())

    # -- ProgressReporter protocol -----------------------------------------

    def on_file_progress(self, progress: FileProgress) -> None:
        """Update (or create) the progress bar for one in-flight file."""
        key = (progress.chat_name, progress.message_id)
        task_id = self._file_task_ids.get(key)
        if task_id is None:
            task_id = self._file_progress.add_task(
                progress.filename,
                total=progress.bytes_total or None,
                chat_name=progress.chat_name,
            )
            self._file_task_ids[key] = task_id
        self._file_progress.update(
            task_id,
            completed=progress.bytes_done,
            total=progress.bytes_total or None,
            description=progress.filename,
        )
        self._refresh()

    def on_file_complete(self, chat_name: str, message_id: int, final_path: Path) -> None:
        """Remove the completed file's progress bar and log a status line."""
        key = (chat_name, message_id)
        task_id = self._file_task_ids.pop(key, None)
        if task_id is not None:
            self._file_progress.remove_task(task_id)
        self._status_line = f"[green]Saved:[/green] {final_path.name}"
        self._refresh()

    def on_file_error(self, chat_name: str, message_id: int, error: str) -> None:
        """Remove the failed file's progress bar and surface the error."""
        key = (chat_name, message_id)
        task_id = self._file_task_ids.pop(key, None)
        if task_id is not None:
            self._file_progress.remove_task(task_id)
        self._status_line = f"[red]Failed[/red] (chat={chat_name} message={message_id}): {error}"
        self._refresh()

    def on_channel_progress(self, progress: ChannelProgress) -> None:
        """Update the summary row for one channel."""
        self._channel_rows[progress.chat_name] = progress
        self._refresh()

    def on_flood_wait(self, seconds: float) -> None:
        """Surface an active FloodWaitError pause in the status line."""
        self._status_line = f"[yellow]FloodWait: pausing {seconds:.1f}s[/yellow]"
        self._refresh()

    # -- context manager ------------------------------------------------

    def __enter__(self) -> "Dashboard":
        """Start the `Live` display."""
        self._live.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the `Live` display, leaving the final frame on screen."""
        self._live.stop()
