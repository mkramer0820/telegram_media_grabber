"""Tests for UploaderWorker: queue building, sequential upload processing,
and dedup/state-tracking against a shared StateStore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage.state import StateStore
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
        self.skipped: list[str] = []
        self.queue_progress: list[UploadQueueProgress] = []

    def on_file_progress(self, progress: UploadFileProgress) -> None:
        self.file_progress.append(progress)

    def on_file_complete(self, filename: str) -> None:
        self.completed.append(filename)

    def on_file_error(self, filename: str, error: str) -> None:
        self.errors.append((filename, error))

    def on_file_skipped(self, filename: str) -> None:
        self.skipped.append(filename)

    def on_queue_progress(self, progress: UploadQueueProgress) -> None:
        self.queue_progress.append(progress)


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_build_queue_lists_files_sorted_and_skips_directories(
    tmp_path: Path, state_store: StateStore
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "b.txt").write_bytes(b"b")
    (upload_dir / "a.txt").write_bytes(b"a")
    (upload_dir / "subdir").mkdir()

    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=upload_dir,
        state_store=state_store,
    )
    queue = worker.build_queue()

    assert [p.name for p in queue] == ["a.txt", "b.txt"]
    state_store.close()


def test_build_queue_returns_empty_list_when_directory_missing(
    tmp_path: Path, state_store: StateStore
) -> None:
    missing = tmp_path / "does_not_exist"
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=missing,
        state_store=state_store,
    )

    assert worker.build_queue() == []
    state_store.close()


async def test_process_queue_uploads_each_file_and_reports_progress(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")
    (upload_dir / "b.txt").write_bytes(b"b")

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
        source_directory=upload_dir,
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert [p.name for p in calls] == ["a.txt", "b.txt"]
    assert reporter.completed == ["a.txt", "b.txt"]
    assert reporter.errors == []
    assert reporter.skipped == []
    assert reporter.queue_progress[-1] == UploadQueueProgress(2, 2, 0, done=True)
    state_store.close()


async def test_process_queue_reports_error_and_continues(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")
    (upload_dir / "b.txt").write_bytes(b"b")

    async def _fake_upload_document(client: object, target_chat: object, file_path: Path, **kwargs: object) -> None:
        if file_path.name == "a.txt":
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(worker_module, "upload_document", _fake_upload_document)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=upload_dir,
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert reporter.errors == [("a.txt", "network unreachable")]
    assert reporter.completed == ["b.txt"]
    assert reporter.queue_progress[-1] == UploadQueueProgress(2, 1, 0, done=True)
    state_store.close()


async def test_process_queue_skips_already_uploaded_file(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")

    calls: list[Path] = []

    async def _fake_upload_document(client: object, target_chat: object, file_path: Path, **kwargs: object) -> None:
        calls.append(file_path)

    monkeypatch.setattr(worker_module, "upload_document", _fake_upload_document)

    from src.uploader.dedup import compute_dedup_key

    await state_store.mark_file_uploaded(
        "@chan", compute_dedup_key(upload_dir / "a.txt"), upload_dir / "a.txt"
    )

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=upload_dir,
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert calls == []  # upload_document never invoked for the skipped file
    assert reporter.skipped == ["a.txt"]
    assert reporter.completed == []
    assert reporter.queue_progress[-1] == UploadQueueProgress(1, 0, 1, done=True)
    state_store.close()


async def test_process_queue_second_run_skips_file_uploaded_in_first_run(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")

    calls: list[Path] = []

    async def _fake_upload_document(client: object, target_chat: object, file_path: Path, **kwargs: object) -> None:
        calls.append(file_path)

    monkeypatch.setattr(worker_module, "upload_document", _fake_upload_document)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=upload_dir,
        state_store=state_store,
        reporter=reporter,
    )
    await worker.process_queue()
    assert len(calls) == 1

    # A fresh worker instance (as a new process run would create) against
    # the same state_store must skip the file it already uploaded.
    second_reporter = RecordingReporter()
    second_worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        target_chat="@chan",
        source_directory=upload_dir,
        state_store=state_store,
        reporter=second_reporter,
    )
    await second_worker.process_queue()

    assert len(calls) == 1  # still just the one call from the first run
    assert second_reporter.skipped == ["a.txt"]
    assert second_reporter.completed == []
    state_store.close()
