"""Live `rich` terminal dashboard for upload mode.

Implements `src.uploader.worker.UploadProgressReporter` so it can be handed
directly to `UploaderWorker` without the uploader layer importing anything
from `ui` (CLAUDE.md Section 1.3 dependency direction). All output here goes
through `rich` — no `print()` (CLAUDE.md Section 3.1).
"""

from __future__ import annotations

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

from src.uploader.worker import UploadFileProgress, UploadQueueProgress


class UploadDashboard:
    """A `Live` dashboard showing queue progress and the active upload.

    Usage:
        with UploadDashboard(console) as dashboard:
            worker = UploaderWorker(..., reporter=dashboard)
            await worker.process_queue()
    """

    def __init__(self, console: Console) -> None:
        """Initialize the dashboard's `rich` components.

        Args:
            console: Shared console instance used for all rendering.
        """
        self._console = console

        self._file_progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        self._file_task_id: TaskID | None = None

        self._queue_status = ""
        self._status_line = ""

        self._live = Live(self._render(), console=console, refresh_per_second=8, transient=False)

    def _render(self) -> Group:
        """Compose the current frame: queue status + active upload progress."""
        panels: list[Panel | Progress] = [self._file_progress]
        if self._queue_status:
            panels.append(Panel(self._queue_status, style="cyan"))
        if self._status_line:
            panels.append(Panel(self._status_line, style="yellow"))
        return Group(*panels)

    def _refresh(self) -> None:
        """Re-render and push the updated frame to the live display."""
        self._live.update(self._render())

    # -- UploadProgressReporter protocol -------------------------------

    def on_file_progress(self, progress: UploadFileProgress) -> None:
        """Update (or create) the progress bar for the in-flight upload."""
        if self._file_task_id is None:
            self._file_task_id = self._file_progress.add_task(
                progress.filename, total=progress.bytes_total or None
            )
        self._file_progress.update(
            self._file_task_id,
            completed=progress.bytes_done,
            total=progress.bytes_total or None,
            description=progress.filename,
        )
        self._refresh()

    def on_file_complete(self, filename: str) -> None:
        """Remove the completed file's progress bar and log a status line."""
        if self._file_task_id is not None:
            self._file_progress.remove_task(self._file_task_id)
            self._file_task_id = None
        self._status_line = f"[green]Uploaded:[/green] {filename}"
        self._refresh()

    def on_file_error(self, filename: str, error: str) -> None:
        """Remove the failed file's progress bar and surface the error."""
        if self._file_task_id is not None:
            self._file_progress.remove_task(self._file_task_id)
            self._file_task_id = None
        self._status_line = f"[red]Failed[/red] ({filename}): {error}"
        self._refresh()

    def on_queue_progress(self, progress: UploadQueueProgress) -> None:
        """Update the overall queue status line."""
        status = "done" if progress.done else "uploading"
        self._queue_status = f"Queue: {progress.files_uploaded}/{progress.files_total} ({status})"
        self._refresh()

    # -- context manager ------------------------------------------------

    def __enter__(self) -> "UploadDashboard":
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
