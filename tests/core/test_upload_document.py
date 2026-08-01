"""Tests for src.core.client.upload_document."""

from __future__ import annotations

from pathlib import Path

import pytest
from telethon.errors import FloodWaitError

from src.core import client as client_module
from src.core.client import upload_document


class FakeChat:
    def __init__(self, chat_id: object) -> None:
        self.id = chat_id


class FakeSendFileClient:
    """Duck-types the subset of TelegramClient upload_document relies on."""

    def __init__(
        self,
        entity_by_id: dict[object, object],
        side_effects: list[Exception] | None = None,
    ) -> None:
        self._entity_by_id = entity_by_id
        self._side_effects = list(side_effects or [])
        self.send_file_calls: list[dict[str, object]] = []

    async def get_entity(self, chat_id: object) -> object:
        return self._entity_by_id[chat_id]

    async def send_file(self, entity: object, file: str, **kwargs: object) -> str:
        if self._side_effects:
            raise self._side_effects.pop(0)
        self.send_file_calls.append({"entity": entity, "file": file, **kwargs})
        return "sent-message"


async def test_upload_document_sends_file_as_document(tmp_path: Path) -> None:
    file_path = tmp_path / "book.pdf"
    file_path.write_bytes(b"data")
    chat = FakeChat("@chan")
    client = FakeSendFileClient(entity_by_id={"@chan": chat})

    result = await upload_document(client, "@chan", file_path, caption="a caption")

    assert result == "sent-message"
    assert len(client.send_file_calls) == 1
    call = client.send_file_calls[0]
    assert call["entity"] is chat
    assert call["file"] == str(file_path)
    assert call["caption"] == "a caption"
    assert call["force_document"] is True


async def test_upload_document_passes_progress_callback(tmp_path: Path) -> None:
    file_path = tmp_path / "book.pdf"
    file_path.write_bytes(b"data")
    client = FakeSendFileClient(entity_by_id={"@chan": FakeChat("@chan")})
    seen: list[tuple[int, int]] = []

    def _progress(current: int, total: int) -> None:
        seen.append((current, total))

    await upload_document(client, "@chan", file_path, progress_callback=_progress)

    assert client.send_file_calls[0]["progress_callback"] is _progress


async def test_upload_document_retries_after_flood_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "book.pdf"
    file_path.write_bytes(b"data")
    client = FakeSendFileClient(
        entity_by_id={"@chan": FakeChat("@chan")},
        side_effects=[FloodWaitError(request=None, capture=5)],  # type: ignore[arg-type]
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    result = await upload_document(client, "@chan", file_path)

    assert result == "sent-message"
    assert len(client.send_file_calls) == 1
    assert sleeps == [5.0 + client_module._FLOOD_WAIT_BUFFER_SECONDS]


async def test_upload_document_raises_after_exhausting_flood_wait_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "book.pdf"
    file_path.write_bytes(b"data")
    side_effects = [
        FloodWaitError(request=None, capture=1)  # type: ignore[arg-type]
        for _ in range(client_module._MAX_FLOOD_WAIT_RETRIES)
    ]
    client = FakeSendFileClient(entity_by_id={"@chan": FakeChat("@chan")}, side_effects=side_effects)

    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(FloodWaitError):
        await upload_document(client, "@chan", file_path)
