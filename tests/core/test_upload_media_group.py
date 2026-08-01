"""Tests for src.core.client.upload_media_group."""

from __future__ import annotations

from pathlib import Path

import pytest
from telethon.errors import FloodWaitError

from src.core import client as client_module
from src.core.client import MEDIA_GROUP_MAX_SIZE, upload_media_group


class FakeChat:
    def __init__(self, chat_id: object) -> None:
        self.id = chat_id


class FakeSendFileClient:
    """Duck-types the subset of TelegramClient upload_media_group relies on."""

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

    async def send_file(self, entity: object, file: object, **kwargs: object) -> object:
        if self._side_effects:
            raise self._side_effects.pop(0)
        self.send_file_calls.append({"entity": entity, "file": file, **kwargs})
        assert isinstance(file, list)
        return [f"sent-{name}" for name in file]


async def test_upload_media_group_sends_all_files_as_one_album(tmp_path: Path) -> None:
    paths = [tmp_path / f"{i}.jpg" for i in range(3)]
    for path in paths:
        path.write_bytes(b"data")
    chat = FakeChat("@chan")
    client = FakeSendFileClient(entity_by_id={"@chan": chat})

    result = await upload_media_group(client, "@chan", paths, caption="album caption")

    assert result == [f"sent-{p}" for p in [str(p) for p in paths]]
    assert len(client.send_file_calls) == 1
    call = client.send_file_calls[0]
    assert call["entity"] is chat
    assert call["file"] == [str(p) for p in paths]
    assert call["caption"] == "album caption"
    assert call["force_document"] is True


async def test_upload_media_group_rejects_empty_batch() -> None:
    with pytest.raises(ValueError):
        await upload_media_group(object(), "@chan", [])  # type: ignore[arg-type]


async def test_upload_media_group_rejects_batch_over_telegram_limit(tmp_path: Path) -> None:
    paths = [tmp_path / f"{i}.jpg" for i in range(MEDIA_GROUP_MAX_SIZE + 1)]
    for path in paths:
        path.write_bytes(b"data")

    with pytest.raises(ValueError):
        await upload_media_group(object(), "@chan", paths)  # type: ignore[arg-type]


async def test_upload_media_group_retries_after_flood_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    for path in paths:
        path.write_bytes(b"data")
    client = FakeSendFileClient(
        entity_by_id={"@chan": FakeChat("@chan")},
        side_effects=[FloodWaitError(request=None, capture=4)],  # type: ignore[arg-type]
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    result = await upload_media_group(client, "@chan", paths)

    assert len(result) == 2
    assert len(client.send_file_calls) == 1
    assert sleeps == [4.0 + client_module._FLOOD_WAIT_BUFFER_SECONDS]
