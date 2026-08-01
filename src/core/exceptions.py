"""Domain-specific exception types for the downloader application."""

from __future__ import annotations


class DownloaderError(Exception):
    """Base class for all application-raised (non-Telethon) errors."""


class AuthenticationError(DownloaderError):
    """Raised when Telethon client authentication fails or is incomplete."""


class DownloadFailedError(DownloaderError):
    """Raised when a single media download cannot be completed.

    Attributes:
        chat_id: Telegram chat/channel ID the message belongs to.
        message_id: Source message ID whose media failed to download.
    """

    def __init__(self, chat_id: int, message_id: int, reason: str) -> None:
        """Initialize with the failing message's identity and a reason.

        Args:
            chat_id: Telegram chat/channel ID the message belongs to.
            message_id: Source message ID whose media failed to download.
            reason: Human-readable explanation of the failure.
        """
        self.chat_id = chat_id
        self.message_id = message_id
        super().__init__(f"Download failed for chat={chat_id} message={message_id}: {reason}")
