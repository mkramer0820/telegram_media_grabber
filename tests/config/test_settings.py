"""Tests for Pydantic settings and channels.yaml parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.settings import ChannelsFile, MediaType, Settings, load_channels_file


VALID_YAML = """
download_root: downloads
max_concurrent_downloads: 3
channels:
  - id: "@some_public_channel"
    name: photos_channel
    media_types: [photo, video]
    output_subdir: photos_channel
  - id: -1001234567890
    name: private_chat_export
    media_types: [document]
    output_subdir: docs
    min_date: "2024-01-01"
"""


def test_load_channels_file_parses_valid_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.yaml"
    config_path.write_text(VALID_YAML, encoding="utf-8")

    channels_file = load_channels_file(config_path)

    assert channels_file.download_root == Path("downloads")
    assert channels_file.max_concurrent_downloads == 3
    assert len(channels_file.channels) == 2

    first = channels_file.channels[0]
    assert first.id == "@some_public_channel"
    assert first.media_types == [MediaType.PHOTO, MediaType.VIDEO]
    assert first.output_subdir == "photos_channel"

    second = channels_file.channels[1]
    assert second.id == -1001234567890
    assert second.min_date == "2024-01-01"


def test_load_channels_file_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_channels_file(tmp_path / "does_not_exist.yaml")


def test_channels_file_defaults_when_minimal() -> None:
    channels_file = ChannelsFile.model_validate({})
    assert channels_file.download_root == Path("downloads")
    assert channels_file.max_concurrent_downloads == 5
    assert channels_file.channels == []


def test_channels_file_rejects_out_of_range_concurrency() -> None:
    with pytest.raises(ValidationError):
        ChannelsFile.model_validate({"max_concurrent_downloads": 0})
    with pytest.raises(ValidationError):
        ChannelsFile.model_validate({"max_concurrent_downloads": 51})


def test_channel_config_requires_output_subdir() -> None:
    with pytest.raises(ValidationError):
        ChannelsFile.model_validate(
            {"channels": [{"id": "@x", "name": "x"}]}
        )


def test_channel_config_defaults_media_types_when_omitted() -> None:
    channels_file = ChannelsFile.model_validate(
        {"channels": [{"id": "@x", "name": "x", "output_subdir": "x"}]}
    )
    assert channels_file.channels[0].media_types == [
        MediaType.PHOTO,
        MediaType.VIDEO,
        MediaType.DOCUMENT,
    ]


def test_settings_rejects_non_positive_api_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "0")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_PHONE", "+15551234567")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_defaults_point_at_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "123456")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_PHONE", "+15551234567")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.tg_session_name == "data/downloader"
    assert settings.state_db_path == Path("data/state.db")


def test_settings_loads_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "999")
    monkeypatch.setenv("TG_API_HASH", "abc123")
    monkeypatch.setenv("TG_PHONE", "+15559998888")
    monkeypatch.setenv("TG_SESSION_NAME", "custom/session")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.tg_api_id == 999
    assert settings.tg_api_hash == "abc123"
    assert settings.tg_phone == "+15559998888"
    assert settings.tg_session_name == "custom/session"


def test_with_channels_loaded_attaches_parsed_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "channels.yaml"
    config_path.write_text(VALID_YAML, encoding="utf-8")

    monkeypatch.setenv("TG_API_ID", "123456")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_PHONE", "+15551234567")

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, channels_config_path=config_path
    )
    loaded = settings.with_channels_loaded()

    assert len(loaded.channels_file.channels) == 2
