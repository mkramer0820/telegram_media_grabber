"""Tests for Telethon client construction and session-file handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInvite, ChatInviteAlready, ChatInvitePeek

from src.config.settings import Settings
from src.core.client import build_client, resolve_entity
from src.core.exceptions import AuthenticationError


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TG_API_ID", "123456")
    monkeypatch.setenv("TG_API_HASH", "test_hash")
    monkeypatch.setenv("TG_PHONE", "+15551234567")
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        tg_session_name=str(tmp_path / "data" / "downloader"),
    )


def test_build_client_creates_session_parent_directory(
    settings: Settings, tmp_path: Path
) -> None:
    session_dir = tmp_path / "data"
    assert not session_dir.exists()

    build_client(settings)

    assert session_dir.exists()


def test_build_client_uses_configured_session_and_credentials(settings: Settings) -> None:
    client = build_client(settings)

    assert client.api_id == 123456
    assert client.api_hash == "test_hash"
    assert str(client.session.filename).startswith(settings.tg_session_name)


def test_build_client_reuses_existing_session_file(settings: Settings) -> None:
    # First build creates the on-disk .session file...
    first_client = build_client(settings)
    session_path = Path(f"{settings.tg_session_name}.session")
    first_client.session.save()
    assert session_path.exists()

    # ...and a second client pointed at the same path picks it up rather
    # than starting fresh (no re-authentication should be required).
    second_client = build_client(settings)
    assert second_client.session.filename == first_client.session.filename
    assert session_path.exists()


def test_build_client_reports_realistic_device_signature(settings: Settings) -> None:
    """Anti-ban: a fixed, realistic device fingerprint (not Telethon's
    generic default, and not randomized per run) should be sent on every
    connection.
    """
    client = build_client(settings)

    init_request = client._init_request
    assert init_request.device_model == "Desktop"
    assert init_request.system_version == "Windows 11"
    assert init_request.app_version == "4.16.8 x64"
    assert init_request.lang_code == "en"


# -- resolve_entity ---------------------------------------------------------


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeInviteClient:
    """Duck-types the subset of TelegramClient resolve_entity relies on."""

    def __init__(self, invite_response: object, entity_by_id: dict[object, object]) -> None:
        self._invite_response = invite_response
        self._entity_by_id = entity_by_id
        self.get_entity_calls: list[object] = []
        self.invite_requests: list[CheckChatInviteRequest] = []

    async def get_entity(self, chat_id: object) -> object:
        self.get_entity_calls.append(chat_id)
        return self._entity_by_id[chat_id]

    async def __call__(self, request: CheckChatInviteRequest) -> object:
        self.invite_requests.append(request)
        return self._invite_response


async def test_resolve_entity_passes_through_numeric_id() -> None:
    client = FakeInviteClient(invite_response=None, entity_by_id={-1001234: FakeChat(-1001234)})

    entity = await resolve_entity(client, -1001234)

    assert entity.id == -1001234
    assert client.get_entity_calls == [-1001234]


async def test_resolve_entity_passes_through_username() -> None:
    client = FakeInviteClient(invite_response=None, entity_by_id={"@chan": FakeChat(1)})

    entity = await resolve_entity(client, "@chan")

    assert entity.id == 1
    assert client.get_entity_calls == ["@chan"]


async def test_resolve_entity_resolves_invite_link_when_already_joined() -> None:
    chat = FakeChat(chat_id=-100999)
    client = FakeInviteClient(invite_response=ChatInviteAlready(chat=chat), entity_by_id={})

    entity = await resolve_entity(client, "https://t.me/+Y30rk8GV9oEyMmNk")

    assert entity is chat
    assert client.invite_requests[0].hash == "Y30rk8GV9oEyMmNk"
    assert client.get_entity_calls == []  # never falls back to get_entity


async def test_resolve_entity_resolves_joinchat_link_when_already_joined() -> None:
    chat = FakeChat(chat_id=-100888)
    client = FakeInviteClient(
        invite_response=ChatInvitePeek(chat=chat, expires=None), entity_by_id={}
    )

    entity = await resolve_entity(client, "https://t.me/joinchat/abcDEF123")

    assert entity is chat


async def test_resolve_entity_raises_when_invite_link_not_yet_joined() -> None:
    not_joined = ChatInvite(title="Some Channel", photo=None, participants_count=0, color=0)
    client = FakeInviteClient(invite_response=not_joined, entity_by_id={})

    with pytest.raises(AuthenticationError):
        await resolve_entity(client, "https://t.me/+Y30rk8GV9oEyMmNk")
