"""Tests for EpisodeVerifier: correcting mistagged episode numbers via Telegram."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from src.config.settings import AudiobookMetadata, ChannelConfig, MediaType
from src.downloader.episode_verifier import EpisodeVerifier, VerifySummary
from src.storage.state import StateStore


def make_channel(name: str = "chan", **overrides: object) -> ChannelConfig:
    defaults: dict[str, object] = dict(
        id=f"@{name}",
        name=name,
        media_types=[MediaType.AUDIO],
        output_subdir=name,
        audiobook_mode=True,
        metadata=AudiobookMetadata(author="Some Author", novel_title="Some Novel"),
    )
    defaults.update(overrides)
    return ChannelConfig.model_validate(defaults)


def _write_dummy_mp3(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 256)


class FakeDocumentAttribute:
    def __init__(self, file_name: str) -> None:
        self.file_name = file_name


class FakeDocument:
    def __init__(self, file_name: str) -> None:
        self.attributes = [FakeDocumentAttribute(file_name)]


class FakeMessage:
    def __init__(self, message_id: int, chat_id: int, raw_filename: str | None) -> None:
        self.id = message_id
        self.chat_id = chat_id
        self.document = FakeDocument(raw_filename) if raw_filename is not None else None


class FakeEntity:
    def __init__(self, entity_id: int) -> None:
        self.id = entity_id


class FakeClient:
    """Duck-types the subset of TelegramClient EpisodeVerifier relies on."""

    def __init__(self, entity_id: int, messages_by_id: dict[int, FakeMessage]) -> None:
        self._entity_id = entity_id
        self._messages_by_id = messages_by_id

    async def get_entity(self, _chat_id: object) -> FakeEntity:
        return FakeEntity(self._entity_id)

    async def get_messages(self, _entity: FakeEntity, ids: list[int]) -> list[FakeMessage | None]:
        return [self._messages_by_id.get(message_id) for message_id in ids]


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


async def test_run_channel_corrects_mismatched_episode_number(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel()
    dest_root = tmp_path / "Audiobooks"
    book_dir = dest_root / "Some Author" / "Some Novel"

    # Currently tagged (wrongly) as episode 999 — e.g. the old message-ID
    # fallback. Telegram's own raw filename says the truth is 1053.
    wrong_path = book_dir / "Some Novel - Ep 0999.mp3"
    _write_dummy_mp3(wrong_path)
    await state_store.record_downloaded_file(42, 999, wrong_path)

    client = FakeClient(
        entity_id=42, messages_by_id={999: FakeMessage(999, 42, "1053.m4a")}
    )
    verifier = EpisodeVerifier(client=client, state_store=state_store, audiobooks_dest_dir=dest_root)  # type: ignore[arg-type]

    summary = await verifier.run_channel(channel)

    assert summary == VerifySummary(checked=1, corrected=1, errors=0)
    assert not wrong_path.exists()
    corrected_path = book_dir / "Some Novel - Ep 1053.mp3"
    assert corrected_path.exists()
    tags = EasyID3(corrected_path)  # type: ignore[no-untyped-call]
    assert tags["tracknumber"] == ["1053"]

    assert await state_store.find_downloaded_record_by_path(wrong_path) is None
    assert await state_store.find_downloaded_record_by_path(corrected_path) == (42, 999)
    state_store.close()


async def test_run_channel_leaves_already_correct_file_untouched(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel()
    dest_root = tmp_path / "Audiobooks"
    book_dir = dest_root / "Some Author" / "Some Novel"

    correct_path = book_dir / "Some Novel - Ep 0042.mp3"
    _write_dummy_mp3(correct_path)
    await state_store.record_downloaded_file(42, 100, correct_path)

    client = FakeClient(entity_id=42, messages_by_id={100: FakeMessage(100, 42, "42.m4a")})
    verifier = EpisodeVerifier(client=client, state_store=state_store, audiobooks_dest_dir=dest_root)  # type: ignore[arg-type]

    summary = await verifier.run_channel(channel)

    assert summary == VerifySummary(checked=1, corrected=0, errors=0)
    assert correct_path.exists()
    state_store.close()


async def test_run_channel_skips_when_telegram_filename_has_no_number(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel()
    dest_root = tmp_path / "Audiobooks"
    book_dir = dest_root / "Some Author" / "Some Novel"

    existing_path = book_dir / "Some Novel - Ep 0999.mp3"
    _write_dummy_mp3(existing_path)
    await state_store.record_downloaded_file(42, 999, existing_path)

    # Telegram's raw filename has no parsable number either -> nothing more
    # trustworthy to correct to; leave the file as-is.
    client = FakeClient(
        entity_id=42, messages_by_id={999: FakeMessage(999, 42, "random_name.m4a")}
    )
    verifier = EpisodeVerifier(client=client, state_store=state_store, audiobooks_dest_dir=dest_root)  # type: ignore[arg-type]

    summary = await verifier.run_channel(channel)

    assert summary == VerifySummary(checked=1, corrected=0, errors=0)
    assert existing_path.exists()
    state_store.close()


async def test_run_channel_skips_when_local_file_missing(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel()
    dest_root = tmp_path / "Audiobooks"
    missing_path = dest_root / "Some Author" / "Some Novel" / "Some Novel - Ep 0999.mp3"
    # Record exists in state, but the file itself was deleted/moved by hand.
    await state_store.record_downloaded_file(42, 999, missing_path)

    client = FakeClient(
        entity_id=42, messages_by_id={999: FakeMessage(999, 42, "1053.m4a")}
    )
    verifier = EpisodeVerifier(client=client, state_store=state_store, audiobooks_dest_dir=dest_root)  # type: ignore[arg-type]

    summary = await verifier.run_channel(channel)

    assert summary == VerifySummary(checked=0, corrected=0, errors=0)
    state_store.close()


async def test_run_channel_returns_zero_summary_when_no_records(
    tmp_path: Path, state_store: StateStore
) -> None:
    channel = make_channel()
    client = FakeClient(entity_id=42, messages_by_id={})
    verifier = EpisodeVerifier(
        client=client, state_store=state_store, audiobooks_dest_dir=tmp_path / "Audiobooks"  # type: ignore[arg-type]
    )

    summary = await verifier.run_channel(channel)

    assert summary == VerifySummary(checked=0, corrected=0, errors=0)
    state_store.close()


def test_verify_summary_addition() -> None:
    a = VerifySummary(checked=1, corrected=2, errors=3)
    b = VerifySummary(checked=4, corrected=5, errors=6)
    assert a + b == VerifySummary(checked=5, corrected=7, errors=9)
