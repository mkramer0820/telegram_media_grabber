"""Centralized, OS-safe filename sanitization (CLAUDE.md Section 2.3).

Every filename derived from Telegram metadata MUST pass through
`sanitize_filename` before touching the filesystem. No other module may
implement its own ad-hoc sanitization.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

_MAX_FILENAME_BYTES = 255

# Characters illegal on Windows, plus control characters, enforced even on
# POSIX systems so output stays portable across platforms.
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_DEFAULT_STEM = "file"


def sanitize_filename(raw_name: str, *, fallback_stem: str = _DEFAULT_STEM) -> str:
    """Sanitize `raw_name` into a safe, portable filename.

    Guarantees:
      - No path traversal: any directory components (`..`, `/`, `\\`,
        leading drive letters) are stripped, leaving only the basename.
      - No characters illegal on Windows, even when running on POSIX.
      - No reserved Windows device names (`CON`, `COM1`, ...) — such names
        are suffixed with `_file`.
      - Result is non-empty and at most 255 bytes (UTF-8), with the
        extension preserved when truncation is necessary.

    Args:
        raw_name: The untrusted, Telegram-derived candidate filename (e.g.
            a caption-derived name or the sender-provided document name).
        fallback_stem: Stem to use if `raw_name` sanitizes down to nothing.

    Returns:
        A filename safe to join onto a trusted base directory. This
        function never returns a path containing a directory separator.
    """
    # Reject path traversal / directory components from both path styles,
    # regardless of the host OS, by taking only the final path segment.
    candidate = PureWindowsPath(PurePosixPath(raw_name).name).name

    candidate = unicodedata.normalize("NFC", candidate)
    candidate = _ILLEGAL_CHARS_RE.sub("_", candidate)
    candidate = candidate.strip(" .")  # trailing dots/spaces are unsafe on Windows

    if not candidate:
        candidate = fallback_stem

    stem, _, ext = candidate.rpartition(".")
    if not stem:
        stem, ext = candidate, ""

    if stem.upper() in _RESERVED_WINDOWS_NAMES:
        stem = f"{stem}_file"

    return _truncate_to_byte_limit(stem, ext)


def _truncate_to_byte_limit(stem: str, ext: str) -> str:
    """Truncate `stem` so `stem.ext` fits within `_MAX_FILENAME_BYTES` UTF-8 bytes.

    Args:
        stem: The filename without its extension.
        ext: The extension without the leading dot (may be empty).

    Returns:
        A `stem.ext` (or bare `stem`) string within the byte budget.
    """
    suffix = f".{ext}" if ext else ""
    suffix_bytes = len(suffix.encode("utf-8"))
    budget = max(_MAX_FILENAME_BYTES - suffix_bytes, 1)

    encoded = stem.encode("utf-8")
    if len(encoded) <= budget:
        return f"{stem}{suffix}"

    truncated = encoded[:budget]
    # Avoid splitting a multi-byte UTF-8 character in half.
    while truncated and (truncated[-1] & 0b1100_0000) == 0b1000_0000:
        truncated = truncated[:-1]
    return f"{truncated.decode('utf-8', errors='ignore')}{suffix}"


def dedup_suffixed_path(base_path: Path) -> Path:
    """Return a non-colliding path by appending " (n)" before the extension.

    Used when two files with different dedup keys would otherwise resolve
    to the same final filename (CLAUDE.md Section 2.4) — the new file is
    suffixed rather than overwriting the existing one.

    Args:
        base_path: The desired final path, which may already exist.

    Returns:
        `base_path` itself if it doesn't exist, otherwise the first
        `name (n).ext` variant that doesn't exist.
    """
    if not base_path.exists():
        return base_path

    stem, suffix = base_path.stem, base_path.suffix
    counter = 1
    while True:
        candidate = base_path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
