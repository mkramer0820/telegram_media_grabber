"""Tests for centralized filename sanitization (CLAUDE.md Section 2.3)."""

from __future__ import annotations

from pathlib import Path

from src.downloader.filenames import dedup_suffixed_path, sanitize_filename


def test_strips_posix_path_traversal() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"


def test_strips_windows_path_components() -> None:
    assert sanitize_filename("C:\\Windows\\evil.exe") == "evil.exe"


def test_strips_leading_slash_absolute_path() -> None:
    assert sanitize_filename("/etc/shadow") == "shadow"


def test_replaces_illegal_characters() -> None:
    result = sanitize_filename('normal file: name?.mp4')
    assert result == "normal file_ name_.mp4"
    for illegal in '<>:"/\\|?*':
        assert illegal not in result


def test_strips_control_characters() -> None:
    result = sanitize_filename("bad\x00name\x1f.txt")
    assert "\x00" not in result
    assert "\x1f" not in result


def test_reserved_windows_device_names_are_suffixed() -> None:
    assert sanitize_filename("CON.txt") == "CON_file.txt"
    assert sanitize_filename("com1") == "com1_file"
    assert sanitize_filename("NUL") == "NUL_file"


def test_reserved_name_check_is_case_insensitive() -> None:
    # Matching against the reserved-name list is case-insensitive, but the
    # original casing of the stem is preserved in the output.
    assert sanitize_filename("lpt1.log") == "lpt1_file.log"


def test_empty_or_whitespace_only_falls_back() -> None:
    assert sanitize_filename("   ...   ") == "file"
    assert sanitize_filename("") == "file"


def test_custom_fallback_stem_is_used() -> None:
    assert sanitize_filename("///", fallback_stem="media") == "media"


def test_truncates_to_byte_limit_preserving_extension() -> None:
    long_name = ("a" * 300) + ".mp4"
    result = sanitize_filename(long_name)
    assert len(result.encode("utf-8")) <= 255
    assert result.endswith(".mp4")


def test_truncation_does_not_split_multibyte_utf8_char() -> None:
    # Each "é" is 2 bytes in UTF-8; ensure truncation never yields a
    # dangling continuation byte that fails to decode.
    long_name = ("é" * 200) + ".txt"
    result = sanitize_filename(long_name)
    result.encode("utf-8").decode("utf-8")  # must not raise
    assert len(result.encode("utf-8")) <= 255


def test_trailing_dots_and_spaces_are_stripped() -> None:
    assert sanitize_filename("report.txt   ...") == "report.txt"


def test_dedup_suffixed_path_returns_original_when_free(tmp_path: Path) -> None:
    target = tmp_path / "video.mp4"
    assert dedup_suffixed_path(target) == target


def test_dedup_suffixed_path_increments_on_collision(tmp_path: Path) -> None:
    target = tmp_path / "video.mp4"
    target.write_bytes(b"existing")

    result = dedup_suffixed_path(target)
    assert result == tmp_path / "video (1).mp4"

    result.write_bytes(b"second")
    next_result = dedup_suffixed_path(target)
    assert next_result == tmp_path / "video (2).mp4"
