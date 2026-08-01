"""Tests for UploaderWorker: multi-job queue building, media-group batching,
and dedup/state-tracking against a shared StateStore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import UploadJobConfig
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


def _patch_media_group_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[object, list[Path]]]:
    """Patch upload_media_group to record (target_chat, file_paths) per batch."""
    calls: list[tuple[object, list[Path]]] = []

    async def _fake_upload_media_group(
        client: object, target_chat: object, file_paths: list[Path], **kwargs: object
    ) -> list[str]:
        calls.append((target_chat, list(file_paths)))
        progress_callback = kwargs.get("progress_callback")
        if callable(progress_callback):
            progress_callback(1, 1)
        return ["sent"] * len(file_paths)

    monkeypatch.setattr(worker_module, "upload_media_group", _fake_upload_media_group)
    return calls


def test_build_queue_lists_files_sorted_within_a_job(
    tmp_path: Path, state_store: StateStore
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "b.txt").write_bytes(b"b")
    (upload_dir / "a.txt").write_bytes(b"a")
    (upload_dir / "subdir").mkdir()

    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan")],
        state_store=state_store,
    )
    queue = worker.build_queue()

    assert [item.file_path.name for item in queue] == ["a.txt", "b.txt"]
    assert all(item.target_chat == "@chan" for item in queue)
    state_store.close()


def test_build_queue_skips_missing_job_directory(tmp_path: Path, state_store: StateStore) -> None:
    missing = tmp_path / "does_not_exist"
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=missing, target_chat="@chan")],
        state_store=state_store,
    )

    assert worker.build_queue() == []
    state_store.close()


def test_build_queue_non_recursive_ignores_subdirectory_files(
    tmp_path: Path, state_store: StateStore
) -> None:
    upload_dir = tmp_path / "uploads"
    (upload_dir / "nested").mkdir(parents=True)
    (upload_dir / "top.txt").write_bytes(b"top")
    (upload_dir / "nested" / "deep.txt").write_bytes(b"deep")

    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan", recursive=False)],
        state_store=state_store,
    )

    assert [item.file_path.name for item in worker.build_queue()] == ["top.txt"]
    state_store.close()


def test_build_queue_recursive_includes_subdirectory_files(
    tmp_path: Path, state_store: StateStore
) -> None:
    upload_dir = tmp_path / "uploads"
    (upload_dir / "nested").mkdir(parents=True)
    (upload_dir / "top.txt").write_bytes(b"top")
    (upload_dir / "nested" / "deep.txt").write_bytes(b"deep")

    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan", recursive=True)],
        state_store=state_store,
    )

    names = sorted(item.file_path.name for item in worker.build_queue())
    assert names == ["deep.txt", "top.txt"]
    state_store.close()


def test_build_queue_routes_multiple_jobs_to_their_own_target_chat(
    tmp_path: Path, state_store: StateStore
) -> None:
    dir_a = tmp_path / "job_a"
    dir_b = tmp_path / "job_b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "a.txt").write_bytes(b"a")
    (dir_b / "b.txt").write_bytes(b"b")

    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[
            UploadJobConfig(source_dir=dir_a, target_chat="@chan_a"),
            UploadJobConfig(source_dir=dir_b, target_chat="@chan_b"),
        ],
        state_store=state_store,
    )

    queue = worker.build_queue()
    routing = {item.file_path.name: item.target_chat for item in queue}
    assert routing == {"a.txt": "@chan_a", "b.txt": "@chan_b"}
    state_store.close()


async def test_process_queue_uploads_batch_and_reports_progress(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")
    (upload_dir / "b.txt").write_bytes(b"b")

    calls = _patch_media_group_recording(monkeypatch)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan")],
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert len(calls) == 1  # both files fit in a single batch of <=10
    target_chat, file_paths = calls[0]
    assert target_chat == "@chan"
    assert [p.name for p in file_paths] == ["a.txt", "b.txt"]

    assert reporter.completed == ["a.txt", "b.txt"]
    assert reporter.errors == []
    assert reporter.skipped == []
    assert reporter.queue_progress[-1] == UploadQueueProgress(2, 2, 0, done=True)
    state_store.close()


async def test_process_queue_chunks_batches_to_media_group_max_size(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    for i in range(12):
        (upload_dir / f"{i:02d}.txt").write_bytes(b"x")

    calls = _patch_media_group_recording(monkeypatch)

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(worker_module.asyncio, "sleep", _fake_sleep)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan")],
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert len(calls) == 2  # 12 files -> batches of 10 and 2
    assert len(calls[0][1]) == 10
    assert len(calls[1][1]) == 2
    assert reporter.queue_progress[-1] == UploadQueueProgress(12, 12, 0, done=True)
    # An explicit pause is inserted between batches (API-limit shielding),
    # but not after the final batch.
    assert sleeps == [worker_module._INTER_BATCH_DELAY_SECONDS]
    state_store.close()


async def test_process_queue_does_not_batch_across_target_chats(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    dir_a = tmp_path / "job_a"
    dir_b = tmp_path / "job_b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "a.txt").write_bytes(b"a")
    (dir_b / "b.txt").write_bytes(b"b")

    calls = _patch_media_group_recording(monkeypatch)

    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(worker_module.asyncio, "sleep", _fake_sleep)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[
            UploadJobConfig(source_dir=dir_a, target_chat="@chan_a"),
            UploadJobConfig(source_dir=dir_b, target_chat="@chan_b"),
        ],
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert len(calls) == 2
    assert calls[0] == ("@chan_a", [dir_a / "a.txt"])
    assert calls[1] == ("@chan_b", [dir_b / "b.txt"])
    state_store.close()


async def test_process_queue_reports_error_for_whole_batch_and_continues(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")
    (upload_dir / "b.txt").write_bytes(b"b")

    async def _fake_upload_media_group(
        client: object, target_chat: object, file_paths: list[Path], **kwargs: object
    ) -> None:
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(worker_module, "upload_media_group", _fake_upload_media_group)

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan")],
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert reporter.errors == [
        ("a.txt", "network unreachable"),
        ("b.txt", "network unreachable"),
    ]
    assert reporter.completed == []
    assert reporter.queue_progress[-1] == UploadQueueProgress(2, 0, 0, done=True)
    state_store.close()


async def test_process_queue_skips_already_uploaded_file(
    tmp_path: Path, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "a.txt").write_bytes(b"a")

    calls = _patch_media_group_recording(monkeypatch)

    from src.uploader.dedup import compute_dedup_key

    await state_store.mark_file_uploaded(
        "@chan", compute_dedup_key(upload_dir / "a.txt"), upload_dir / "a.txt"
    )

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[UploadJobConfig(source_dir=upload_dir, target_chat="@chan")],
        state_store=state_store,
        reporter=reporter,
    )

    await worker.process_queue()

    assert calls == []  # upload_media_group never invoked for the skipped file
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

    calls = _patch_media_group_recording(monkeypatch)

    job = UploadJobConfig(source_dir=upload_dir, target_chat="@chan")

    reporter = RecordingReporter()
    worker = UploaderWorker(
        client=object(),  # type: ignore[arg-type]
        upload_jobs=[job],
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
        upload_jobs=[job],
        state_store=state_store,
        reporter=second_reporter,
    )
    await second_worker.process_queue()

    assert len(calls) == 1  # still just the one batch from the first run
    assert second_reporter.skipped == ["a.txt"]
    assert second_reporter.completed == []
    state_store.close()
