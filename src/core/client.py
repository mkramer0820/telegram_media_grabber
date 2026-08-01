"""Telethon client construction and authentication.

The client is built once from `Settings` (dependency injection — no module
here reads environment variables or files directly) and reuses the existing
`.session` file on disk so that, after the first interactive login, every
subsequent run connects without prompting for a login code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from src.config.settings import Settings
from src.core.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

try:
    import cryptg  # noqa: F401

    _CRYPTG_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _CRYPTG_AVAILABLE = False

# Anti-ban: identify as a real, common Telegram Desktop install rather than
# Telethon's default generic signature. A device fingerprint that never
# changes across runs (fixed values, not randomized per-session) looks like
# a normal returning user to Telegram's abuse heuristics; a client that
# reports no device info, or a different one every run, reads as automation.
_DEVICE_MODEL = "Desktop"
_SYSTEM_VERSION = "Windows 11"
_APP_VERSION = "4.16.8 x64"
_LANG_CODE = "en"


def build_client(settings: Settings) -> TelegramClient:
    """Construct a `TelegramClient` configured from `settings`.

    The session file path comes from `settings.tg_session_name` (backed by
    the `TG_SESSION_NAME` env var, default `data/downloader`). Telethon
    stores this as `<tg_session_name>.session`; if that file already exists
    from a prior login, the returned client will reuse it and no fresh
    authentication is required.

    `cryptg` is imported eagerly (see module import above) purely so we can
    log whether Telethon will use its accelerated native encryption path for
    media downloads/uploads — Telethon auto-detects and uses it whenever the
    package is importable, no explicit wiring is needed here.

    Args:
        settings: Application settings providing Telegram API credentials
            and the session file location.

    Returns:
        A configured, not-yet-connected `TelegramClient`.
    """
    session_path = settings.tg_session_name
    # Ensure the parent directory of the session file exists so Telethon can
    # create/read it regardless of where TG_SESSION_NAME points (e.g. a
    # pre-existing `data/downloader.session` from a prior deployment).
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)

    if _CRYPTG_AVAILABLE:
        logger.info("cryptg is available: Telethon will use accelerated encryption.")
    else:
        logger.warning(
            "cryptg is not installed: falling back to Telethon's pure-Python "
            "encryption, which is slower for large media downloads."
        )

    client = TelegramClient(
        session_path,
        settings.tg_api_id,
        settings.tg_api_hash,
        connection_retries=10,
        retry_delay=1,
        auto_reconnect=True,
        flood_sleep_threshold=0,  # handled explicitly by downloader workers
        device_model=_DEVICE_MODEL,
        system_version=_SYSTEM_VERSION,
        app_version=_APP_VERSION,
        lang_code=_LANG_CODE,
    )
    return client


async def connect_and_authenticate(client: TelegramClient, settings: Settings, console: Console) -> None:
    """Connect `client` and ensure it is authenticated, reusing any existing session.

    If a valid `.session` file already exists, this completes silently with
    no user interaction. Otherwise it drives Telethon's interactive login
    flow (code, and 2FA password if enabled), prompting via `console`.

    Args:
        client: A client built by `build_client`.
        settings: Application settings, used for the phone number on first
            login.
        console: Shared `rich` console for any interactive login prompts.

    Raises:
        AuthenticationError: If authentication cannot be completed.
    """
    await client.connect()

    if await client.is_user_authorized():
        logger.info("Reusing existing Telethon session; no login required.")
        return

    console.print(
        "[yellow]No valid session found — interactive Telegram login required.[/yellow]"
    )
    try:
        await client.send_code_request(settings.tg_phone)
        code = console.input("Enter the login code sent to your Telegram app: ")
        try:
            await client.sign_in(settings.tg_phone, code)
        except SessionPasswordNeededError:
            password = console.input("Two-factor password: ", password=True)
            await client.sign_in(password=password)
    except Exception as exc:  # noqa: BLE001 - boundary: convert to domain error
        logger.exception("Telegram authentication failed.")
        raise AuthenticationError(f"Failed to authenticate with Telegram: {exc}") from exc

    logger.info("Telegram authentication successful; session saved to %s.session", settings.tg_session_name)
