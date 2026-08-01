"""Backend logging setup: rotating file handler only, never stdout/stderr.

Per CLAUDE.md Section 3, attaching a StreamHandler here is forbidden — it
would corrupt `rich` Live displays and progress bars rendered elsewhere in
the `ui` layer.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5


def configure_logging(log_file_path: Path, level: int = logging.INFO) -> None:
    """Configure the root logger to write only to a rotating file.

    Args:
        log_file_path: Destination path for the rotating log file.
        level: Minimum log level to record.
    """
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_file_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
