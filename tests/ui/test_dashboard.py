"""Tests for the rich Live dashboard's ProgressReporter implementation."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from src.downloader.worker import ChannelProgress, FileProgress
from src.ui.dashboard import Dashboard


def _console() -> Console:
    # Render into an in-memory buffer instead of the real terminal so tests
    # don't depend on a TTY, and never touch print() (CLAUDE.md Section 3.1).
    return Console(file=io.StringIO(), force_terminal=True, width=100)


def test_dashboard_handles_full_progress_lifecycle(tmp_path: Path) -> None:
    console = _console()
    with Dashboard(console) as dashboard:
        dashboard.on_channel_progress(ChannelProgress("chan", 1, 0, done=False))
        dashboard.on_file_progress(FileProgress("chan", 1, "video.mp4", 0, 100))
        dashboard.on_file_progress(FileProgress("chan", 1, "video.mp4", 50, 100))
        dashboard.on_flood_wait(12.5)
        dashboard.on_file_complete("chan", 1, tmp_path / "video.mp4")
        dashboard.on_channel_progress(ChannelProgress("chan", 1, 1, done=True))

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "chan" in output


def test_dashboard_reports_file_errors_without_raising(tmp_path: Path) -> None:
    console = _console()
    with Dashboard(console) as dashboard:
        dashboard.on_file_progress(FileProgress("chan", 2, "broken.mp4", 0, 10))
        dashboard.on_file_error("chan", 2, "network unreachable")

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "network unreachable" in output
