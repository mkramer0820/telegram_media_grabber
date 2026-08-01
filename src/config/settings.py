"""Application settings: merges `.env` secrets with `config/channels.yaml`.

`Settings` is the single source of truth for runtime configuration. It is
constructed exactly once in `src/main.py` and passed explicitly to every
layer that needs it (per CLAUDE.md's ban on module-level mutable state).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MediaType(str, Enum):
    """Media kinds that can be requested for download from a chat."""

    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"


class AudiobookMetadata(BaseModel):
    """Author/title metadata attached to an `audiobook_mode` channel.

    Consumed by `src.downloader.audiobook_processor` to populate ID3/MP4
    tags and to build the `{author}/{novel_title}/...` destination layout.
    """

    # extra="forbid": a typo'd or misplaced key (e.g. `min_date` nested
    # here instead of on the channel itself) must fail config loading
    # loudly, not silently no-op — see ChannelConfig for the same rule.
    model_config = ConfigDict(extra="forbid")

    author: str = Field(..., description="Tagged as Artist/AlbumArtist.")
    novel_title: str = Field(..., description="Tagged as Album; also the destination folder.")


class ChannelConfig(BaseModel):
    """A single download target declared in `config/channels.yaml`."""

    # extra="forbid": Pydantic's default is to silently ignore unknown
    # keys, which turns a config typo (e.g. `audio_book_mode` instead of
    # `audiobook_mode`) into a feature that quietly never runs. Fail fast
    # at config-load time instead.
    model_config = ConfigDict(extra="forbid")

    id: int | str = Field(..., description="Telegram chat ID or @username.")
    name: str = Field(..., description="Human-readable label used in logs/UI.")
    media_types: list[MediaType] = Field(
        default_factory=lambda: [MediaType.PHOTO, MediaType.VIDEO, MediaType.DOCUMENT]
    )
    output_subdir: str = Field(..., description="Subdirectory under download_root.")
    min_date: str | None = Field(
        default=None, description="ISO-8601 date; skip messages older than this."
    )
    audiobook_mode: bool = Field(
        default=False,
        description="If true, downloaded audio is tagged and relocated by audiobook_processor.",
    )
    metadata: AudiobookMetadata | None = Field(
        default=None, description="Required when audiobook_mode is true."
    )

    @model_validator(mode="after")
    def _require_metadata_when_audiobook_mode(self) -> "ChannelConfig":
        """Fail config loading fast rather than silently skipping tagging later."""
        if self.audiobook_mode and self.metadata is None:
            raise ValueError(
                f"Channel '{self.name}' has audiobook_mode=true but no `metadata` "
                "(author/novel_title) block."
            )
        return self


class UploadJobConfig(BaseModel):
    """A single upload target declared in `config/channels.yaml`'s `upload_jobs` list.

    Multiple jobs let upload mode route different local directories to
    different Telegram chats in one run (e.g. one folder per destination
    channel), optionally scanning each directory's subfolders too.
    """

    model_config = ConfigDict(extra="forbid")

    source_dir: Path = Field(..., description="Local directory scanned for files to upload.")
    target_chat: int | str = Field(..., description="Destination chat ID or @username.")
    recursive: bool = Field(
        default=False,
        description="If true, scan source_dir and all its subdirectories for files.",
    )


class ChannelsFile(BaseModel):
    """Schema of the top-level `config/channels.yaml` document."""

    model_config = ConfigDict(extra="forbid")

    download_root: Path = Field(default=Path("downloads"))
    max_concurrent_downloads: int = Field(default=5, ge=1, le=50)
    channels: list[ChannelConfig] = Field(default_factory=list)
    upload_jobs: list[UploadJobConfig] = Field(
        default_factory=list,
        description="Upload targets for upload mode; each maps a source_dir to a target_chat.",
    )


class Settings(BaseSettings):
    """Merged runtime configuration for the application.

    Secrets and connection parameters come from environment variables / a
    `.env` file (`TG_*`). The channel list and download policy come from a
    separate YAML file, loaded via `load_channels_file` and attached here so
    callers only ever depend on one `Settings` object.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tg_api_id: int = Field(..., description="Telegram API ID from my.telegram.org.")
    tg_api_hash: str = Field(..., description="Telegram API hash from my.telegram.org.")
    tg_phone: str = Field(..., description="Phone number used to log in to Telegram.")
    tg_session_name: str = Field(
        default="data/downloader",
        description="Path (without extension) to the Telethon .session file.",
    )
    channels_config_path: Path = Field(
        default=Path("config/channels.yaml"),
        description="Path to the YAML file declaring download targets.",
    )
    state_db_path: Path = Field(
        default=Path("data/state.db"),
        description="Path to the SQLite database used for state tracking.",
    )
    log_file_path: Path = Field(
        default=Path("logs/app.log"),
        description="Path to the rotating backend log file.",
    )
    audiobooks_dest_dir: Path = Field(
        default=Path("downloads/Audiobooks"),
        description=(
            "Destination root for audiobook_mode channels. Override via "
            "AUDIOBOOKS_DEST_DIR to point at an external/NAS path, e.g. "
            "P:\\Audiobooks or /mnt/plex/Audiobooks."
        ),
    )

    channels_file: ChannelsFile = Field(default_factory=ChannelsFile, exclude=True)

    @field_validator("tg_api_id")
    @classmethod
    def _validate_api_id(cls, value: int) -> int:
        """Reject an obviously-unset placeholder API ID."""
        if value <= 0:
            raise ValueError("TG_API_ID must be a positive integer.")
        return value

    def with_channels_loaded(self) -> "Settings":
        """Return a copy of this Settings with `channels_file` populated from disk.

        Raises:
            FileNotFoundError: If `channels_config_path` does not exist.
        """
        return self.model_copy(
            update={"channels_file": load_channels_file(self.channels_config_path)}
        )


def _construct_settings() -> Settings:
    """Construct `Settings`, loading required fields from env/.env.

    `Settings()` takes no explicit arguments by design: `pydantic-settings`
    populates every field from the environment / `.env` file at runtime.
    mypy cannot see through that dynamic behavior, hence the ignore below.
    """
    return Settings()  # type: ignore[call-arg]


def load_channels_file(path: Path) -> ChannelsFile:
    """Load and validate the channels YAML config at `path`.

    Args:
        path: Filesystem path to the YAML document.

    Returns:
        A validated `ChannelsFile` instance.

    Raises:
        FileNotFoundError: If `path` does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Channels config not found: {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ChannelsFile.model_validate(raw)


def get_settings() -> Settings:
    """Build the application `Settings`, loading `.env` and `channels.yaml`.

    This is the single entry point `main.py` should call to obtain
    configuration; no other module should construct `Settings` directly.
    """
    return _construct_settings().with_channels_loaded()
