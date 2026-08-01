"""Tests for DownloadManager: atomic renames and FloodWaitError retry flow.

Telethon's client/message objects are replaced with lightweight fakes so
these tests exercise our own retry/atomic-write logic without a real
network connection (CLAUDE.md's async/concurrency rules are what's under
test here, not Telethon itself).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
from telethon.errors import FloodWaitError

from src.config.settings import AudiobookMetadata, ChannelConfig, MediaType
from src.downloader.worker import ChannelProgress, DownloadManager, FileProgress
from src.storage.state import StateStore


def make_channel(name: str = "chan", **overrides: object) -> ChannelConfig:
    """Build a minimal ChannelConfig for tests, with overridable fields."""
    defaults: dict[str, object] = dict(
        id=f"@{name}",
        name=name,
        media_types=[MediaType.PHOTO, MediaType.VIDEO, MediaType.DOCUMENT],
        output_subdir=name,
    )
    defaults.update(overrides)
    return ChannelConfig.model_validate(defaults)


class FakeDocumentAttribute:
    def __init__(self, file_name: str) -> None:
        self.file_name = file_name


class FakeDocument:
    def __init__(self, file_name: str) -> None:
        self.attributes = [FakeDocumentAttribute(file_name)]


class FakeMessage:
    """Duck-types the subset of telethon.tl.custom.message.Message we use."""

    def __init__(
        self,
        message_id: int,
        chat_id: int,
        *,
        document: FakeDocument | None = None,
        photo: object | None = None,
        content: bytes = b"fake media bytes",
        side_effects: list[Exception] | None = None,
        date: datetime | None = None,
    ) -> None:
        self.id = message_id
        self.chat_id = chat_id
        self.document = document
        self.photo = photo
        self.video = None
        self.audio = None
        self.date = date or datetime.now(timezone.utc)
        self._content = content
        self._side_effects = list(side_effects or [])
        self.download_attempts = 0

    async def download_media(self, file: str, progress_callback: object = None) -> str:
        self.download_attempts += 1
        if self._side_effects:
            raise self._side_effects.pop(0)
        Path(file).write_bytes(self._content)
        if callable(progress_callback):
            progress_callback(len(self._content), len(self._content))
        return file


class RecordingReporter:
    def __init__(self) -> None:
        self.file_progress: list[FileProgress] = []
        self.completed: list[tuple[str, int, Path]] = []
        self.errors: list[tuple[str, int, str]] = []
        self.channel_progress: list[ChannelProgress] = []
        self.flood_waits: list[float] = []

    def on_file_progress(self, progress: FileProgress) -> None:
        self.file_progress.append(progress)

    def on_file_complete(self, chat_name: str, message_id: int, final_path: Path) -> None:
        self.completed.append((chat_name, message_id, final_path))

    def on_file_error(self, chat_name: str, message_id: int, error: str) -> None:
        self.errors.append((chat_name, message_id, error))

    def on_channel_progress(self, progress: ChannelProgress) -> None:
        self.channel_progress.append(progress)

    def on_flood_wait(self, seconds: float) -> None:
        self.flood_waits.append(seconds)


class FakeEntity:
    def __init__(self, entity_id: int) -> None:
        self.id = entity_id


class FakeClient:
    def __init__(self, entity_id: int, messages: list[FakeMessage]) -> None:
        self._entity_id = entity_id
        self._messages = messages

    async def get_entity(self, _chat_id: object) -> FakeEntity:
        return FakeEntity(self._entity_id)

    async def iter_messages(
        self, _entity: FakeEntity, min_id: int = 0
    ) -> AsyncIterator[FakeMessage]:
        for message in self._messages:
            if message.id > min_id:
                yield message


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


@pytest.fixture
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Prevent tests from actually sleeping in real time, while recording
    every requested duration so anti-ban pacing/backoff can be asserted on.
    """
    import asyncio

    calls: list[float] = []

    async def _instant_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    return calls


@pytest.fixture(autouse=True)
def no_real_sleep(sleep_calls: list[float]) -> None:
    """Autouse alias so every test gets instant sleeps even without needing
    to assert on the recorded durations.
    """


async def test_download_one_writes_atomically_and_records_state(
    tmp_path: Path, state_store: StateStore
) -> None:
    message = FakeMessage(message_id=1, chat_id=42, document=FakeDocument("report.pdf"))
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=object(),  # unused by _download_one directly
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("photos_channel"), 42, message, output_dir)

    final_path = output_dir / "report.pdf"
    assert final_path.exists()
    assert final_path.read_bytes() == b"fake media bytes"
    # No leftover .tmp file after a successful atomic rename.
    assert list(output_dir.glob("*.tmp")) == []

    assert await state_store.is_downloaded(42, 1) is True
    assert reporter.completed == [("photos_channel", 1, final_path)]
    state_store.close()


async def test_download_one_sanitizes_unsafe_filename(
    tmp_path: Path, state_store: StateStore
) -> None:
    message = FakeMessage(
        message_id=2, chat_id=42, document=FakeDocument("../../evil:name?.exe")
    )
    manager = DownloadManager(
        client=object(),
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("chan"), 42, message, output_dir)

    files = list(output_dir.iterdir())
    assert len(files) == 1
    assert ".." not in files[0].name
    assert ":" not in files[0].name
    assert "?" not in files[0].name
    state_store.close()


async def test_download_one_leaves_no_tmp_on_permanent_failure(
    tmp_path: Path, state_store: StateStore
) -> None:
    message = FakeMessage(
        message_id=3,
        chat_id=42,
        document=FakeDocument("broken.mp4"),
        side_effects=[RuntimeError("boom")] * 10,  # exceed retry budget
    )
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=object(),
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("chan"), 42, message, output_dir)

    assert list(output_dir.iterdir()) == []  # no .tmp or final file left behind
    assert await state_store.is_downloaded(42, 3) is False
    assert len(reporter.errors) == 1
    assert reporter.errors[0][0] == "chan"
    assert reporter.errors[0][1] == 3
    state_store.close()


async def test_flood_wait_error_triggers_retry_and_eventually_succeeds(
    tmp_path: Path, state_store: StateStore
) -> None:
    flood_error = FloodWaitError(request=None, capture=7)
    message = FakeMessage(
        message_id=4,
        chat_id=42,
        document=FakeDocument("video.mp4"),
        side_effects=[flood_error],
    )
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=object(),
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("chan"), 42, message, output_dir)

    # First call raised FloodWaitError, second call (the retry) succeeded.
    assert message.download_attempts == 2
    assert (output_dir / "video.mp4").exists()
    assert reporter.flood_waits, "on_flood_wait should have been called"
    assert reporter.flood_waits[0] >= flood_error.seconds
    assert reporter.completed == [("chan", 4, output_dir / "video.mp4")]
    state_store.close()


async def test_flood_wait_error_gives_up_after_max_retries(
    tmp_path: Path, state_store: StateStore
) -> None:
    flood_error = FloodWaitError(request=None, capture=1)
    message = FakeMessage(
        message_id=5,
        chat_id=42,
        document=FakeDocument("video.mp4"),
        side_effects=[flood_error] * 10,
    )
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=object(),
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("chan"), 42, message, output_dir)

    assert list(output_dir.iterdir()) == []
    assert len(reporter.errors) == 1
    assert await state_store.is_downloaded(42, 5) is False
    state_store.close()


async def test_process_channel_skips_already_downloaded_and_updates_last_id(
    tmp_path: Path, state_store: StateStore
) -> None:
    await state_store.record_downloaded_file(99, 1, tmp_path / "already.mp4")

    messages = [
        FakeMessage(1, 99, document=FakeDocument("one.mp4")),  # already downloaded
        FakeMessage(2, 99, document=FakeDocument("two.mp4")),
        FakeMessage(3, 99, photo=object()),  # no document, but matches "photo"
    ]
    client = FakeClient(entity_id=99, messages=messages)
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=client,
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    channel = ChannelConfig(
        id="@chan",
        name="chan",
        media_types=[MediaType.PHOTO, MediaType.VIDEO, MediaType.DOCUMENT],
        output_subdir="chan",
    )

    await manager._process_channel(channel)

    assert await state_store.get_last_message_id(99) == 3
    downloaded_names = {p[2].name for p in reporter.completed}
    assert downloaded_names == {"two.mp4", "99_3"}


async def test_process_channel_stops_at_min_date_cutoff(
    tmp_path: Path, state_store: StateStore
) -> None:
    now = datetime.now(timezone.utc)
    # Newest-first order, matching Telethon's default iteration order.
    messages = [
        FakeMessage(3, 77, document=FakeDocument("newest.mp3"), date=now - timedelta(days=1)),
        FakeMessage(2, 77, document=FakeDocument("middle.mp3"), date=now - timedelta(days=3)),
        FakeMessage(1, 77, document=FakeDocument("oldest.mp3"), date=now - timedelta(days=10)),
    ]
    client = FakeClient(entity_id=77, messages=messages)
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=client,
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    cutoff = (now - timedelta(days=5)).date().isoformat()
    channel = make_channel("chan", min_date=cutoff)

    await manager._process_channel(channel)

    downloaded_names = {p[2].name for p in reporter.completed}
    assert downloaded_names == {"newest.mp3", "middle.mp3"}
    assert "oldest.mp3" not in downloaded_names
    # last_message_id reflects only messages actually scanned before the
    # date-cutoff break, not the oldest message in the channel.
    assert await state_store.get_last_message_id(77) == 3


async def test_process_channel_with_no_min_date_downloads_everything(
    tmp_path: Path, state_store: StateStore
) -> None:
    now = datetime.now(timezone.utc)
    messages = [
        FakeMessage(2, 88, document=FakeDocument("recent.mp3"), date=now - timedelta(days=1)),
        FakeMessage(1, 88, document=FakeDocument("ancient.mp3"), date=now - timedelta(days=3650)),
    ]
    client = FakeClient(entity_id=88, messages=messages)
    reporter = RecordingReporter()
    manager = DownloadManager(
        client=client,
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
        reporter=reporter,
    )
    channel = make_channel("chan")  # min_date defaults to None

    await manager._process_channel(channel)

    downloaded_names = {p[2].name for p in reporter.completed}
    assert downloaded_names == {"recent.mp3", "ancient.mp3"}


async def test_flood_wait_sleeps_exact_server_duration_plus_fixed_buffer(
    tmp_path: Path, state_store: StateStore, sleep_calls: list[float]
) -> None:
    """CLAUDE.md Section 4.6 / anti-ban discipline: the flood wait must be
    the exact server-requested duration plus a fixed 2s buffer — never a
    growing multiple that would drift away from what Telegram asked for.
    """
    flood_error = FloodWaitError(request=None, capture=10)
    message = FakeMessage(
        message_id=6,
        chat_id=42,
        document=FakeDocument("video.mp4"),
        side_effects=[flood_error],
    )
    manager = DownloadManager(
        client=object(),
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("chan"), 42, message, output_dir)

    # The flood-wait sleep (not the inter-download jitter, which is a much
    # smaller, separate call) must be exactly seconds + 2.0 buffer.
    assert 12.0 in sleep_calls
    state_store.close()


async def test_inter_download_jitter_pause_is_within_configured_range(
    tmp_path: Path, state_store: StateStore, sleep_calls: list[float]
) -> None:
    """Anti-ban pacing: a randomized 2.0-5.0s pause follows every download
    attempt (success or failure) before the semaphore slot is released.
    """
    message = FakeMessage(message_id=7, chat_id=42, document=FakeDocument("book.mp3"))
    manager = DownloadManager(
        client=object(),
        state_store=state_store,
        download_root=tmp_path,
        max_concurrent_downloads=3,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    await manager._download_one(make_channel("chan"), 42, message, output_dir)

    assert len(sleep_calls) == 1
    assert 2.0 <= sleep_calls[0] <= 5.0
    state_store.close()
