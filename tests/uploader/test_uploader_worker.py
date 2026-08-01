"""Tests for UploaderWorker: queue building and sequential upload processing."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.uploader import worker as worker_module
from src.uploader.worker import (
    UploaderWorker,
    UploadFileProgress,
    UploadQueueProgress,
)


class RecordingReporter:
    """Captures every callback invocation for assertions."""

    def __init__(self) -> None:
        self.file_progress: list[UploadFileProgress] = []
        self.completed: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.queue_progress: list[UploadQueueProgress] = []

    def on_file_progress(self, progress: UploadFileProgress) -> None:
        self.file_progress.append(progress)

    def on_file_complete(self, filename: str) -> None:
        self.completed.append(filename)

    def on_file_error(self, filename: str, error: str) -> None:
        self.errors.append((filename, error))

    def on_queue_progress(self, progress: UploadQueueProgress) -> None:
        self.queue_progress.append(progress)


def test_build_queue_lists_files_sorted_and_skips_directories(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_bytes(b"b")
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "subdir").mkdir()

    worker = UploaderWorker(client=object(), target_chat="@chan", source_directory=tmp_path)  # type: ignore[arg-type]
    queue = worker.build_queue()

    assert [p.name for p in queue] == ["a.txt", "b.txt"]


def test_build_queue_returns_empty_list_when_directory_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    worker = UploaderWorker(client=object(), target_chat="@chan", source_directory=missing)  # type: ignore[arg-type]

    assert worker.build_queue() == []


async def test_process_queue_uploads_each_file_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")

    calls: list[Path] = []

    async def _fake_upload_document(client: object, target_chat: object, file_path: Path, **kwargs: object) -> None:
        calls.append(file_path)
        progress_callback = kwargs.get("progress_callback")
        if callable(progress_callback):
            progress_callback(1, 1)

    monkeypatch.setattr(worker_module, "upload_document", _fake_upload_document)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=tmp_path,
        reporter=reporter,
    )

    await worker.process_queue()

    assert [p.name for p in calls] == ["a.txt", "b.txt"]
    assert reporter.completed == ["a.txt", "b.txt"]
    assert reporter.errors == []
    assert reporter.queue_progress[-1] == UploadQueueProgress(2, 2, done=True)


async def test_process_queue_reports_error_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")

    async def _fake_upload_document(client: object, target_chat: object, file_path: Path, **kwargs: object) -> None:
        if file_path.name == "a.txt":
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(worker_module, "upload_document", _fake_upload_document)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=tmp_path,
        reporter=reporter,
    )

    await worker.process_queue()

    assert reporter.errors == [("a.txt", "network unreachable")]
    assert reporter.completed == ["b.txt"]
    assert reporter.queue_progress[-1] == UploadQueueProgress(2, 1, done=True)
