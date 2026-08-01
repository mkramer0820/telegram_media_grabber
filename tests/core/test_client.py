"""Tests for Telethon client construction and session-file handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import Settings
from src.core.client import build_client


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
