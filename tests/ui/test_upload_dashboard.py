"""Tests for the rich Live dashboard's UploadProgressReporter implementation."""

from __future__ import annotations

import io

from rich.console import Console

from src.ui.upload_dashboard import UploadDashboard
from src.uploader.worker import UploadFileProgress, UploadQueueProgress


def _console() -> Console:
    # Render into an in-memory buffer instead of the real terminal so tests
    # don't depend on a TTY, and never touch print() (CLAUDE.md Section 3.1).
    return Console(file=io.StringIO(), force_terminal=True, width=100)


def test_upload_dashboard_handles_full_progress_lifecycle() -> None:
    console = _console()
    with UploadDashboard(console) as dashboard:
        dashboard.on_queue_progress(UploadQueueProgress(2, 0, done=False))
        dashboard.on_file_progress(UploadFileProgress("book.pdf", 0, 100))
        dashboard.on_file_progress(UploadFileProgress("book.pdf", 50, 100))
        dashboard.on_file_complete("book.pdf")
        dashboard.on_queue_progress(UploadQueueProgress(2, 2, done=True))

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "book.pdf" in output


def test_upload_dashboard_reports_file_errors_without_raising() -> None:
    console = _console()
    with UploadDashboard(console) as dashboard:
        dashboard.on_file_progress(UploadFileProgress("broken.zip", 0, 10))
        dashboard.on_file_error("broken.zip", "network unreachable")

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "network unreachable" in output
