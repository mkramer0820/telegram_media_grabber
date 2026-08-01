"""Post-processing for `audiobook_mode` channels: tag, rename, and relocate.

Invoked by `src.downloader.worker` immediately after a chapter file's atomic
`.tmp` -> final rename succeeds (CLAUDE.md Section 2.1/2.5 still apply up to
that point). From there this module:

  1. Extracts an episode/chapter number and optional subtitle from the raw
     filename (regex-based; falls back to the Telegram message ID).
  2. Embeds Artist/AlbumArtist/Album/Title/Track tags via `mutagen`.
  3. Moves the file into `{dest_root}/{author}/{novel_title}/...` using
     `shutil.move` (not `os.rename`/`Path.replace`) so relocating onto a
     different filesystem or network mount never raises `EXDEV`.

Deliberately out of scope: concatenating chapters into a single `.m4b`.
Chapters stay individual files; no `ffmpeg` dependency is introduced.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import ID3NoHeaderError
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4

from src.config.settings import AudiobookMetadata
from src.downloader.filenames import dedup_suffixed_path, sanitize_filename

logger = logging.getLogger(__name__)

_TAGGABLE_EXTENSIONS = {".mp3", ".m4b", ".m4a"}

# Matches "Ep 2027 - The Strength of the Wolf" (case-insensitive, "Ep."/"Ep"/
# "Episode" all accepted) anywhere in the filename stem, capturing the
# episode number and everything after the separator as a candidate subtitle.
_EPISODE_PATTERN = re.compile(
    r"ep(?:isode)?\.?\s*(?P<episode>\d+)\s*[-:]\s*(?P<rest>.+)$",
    re.IGNORECASE,
)

# Peels a trailing "-UploaderTag" signature (a hyphen directly followed by
# a single space-free token at the very end) off an extracted subtitle,
# e.g. "The Strength of the Wolf-XtreamStories" -> "The Strength of the Wolf".
# Deliberately requires *no* space before the hyphen so genuine subtitle
# text like "...Part 2" (space-separated) is left untouched.
_TRAILING_UPLOADER_TAG_PATTERN = re.compile(r"^(?P<subtitle>.+?)-[A-Za-z0-9]+$")


@dataclass(frozen=True, slots=True)
class EpisodeInfo:
    """Extracted (or fallback) episode identity for one chapter file."""

    episode: int
    subtitle: str | None


def extract_episode_info(raw_filename: str, fallback_episode: int) -> EpisodeInfo:
    """Extract episode number and subtitle from a raw Telegram filename.

    Args:
        raw_filename: The original filename (with or without extension),
            e.g. "__Shadow Slave.Ep 2027 - The Strength of the Wolf-XtreamStories.mp3".
        fallback_episode: Used as the episode number when no `Ep <n>`-style
            pattern is found (typically the Telegram message ID).

    Returns:
        An `EpisodeInfo` with the parsed episode number and subtitle (or
        `None` subtitle if none could be isolated).
    """
    stem = Path(raw_filename).stem
    match = _EPISODE_PATTERN.search(stem)
    if match is None:
        return EpisodeInfo(episode=fallback_episode, subtitle=None)

    episode = int(match.group("episode"))
    rest = match.group("rest").strip()

    tag_match = _TRAILING_UPLOADER_TAG_PATTERN.match(rest)
    subtitle = tag_match.group("subtitle").strip() if tag_match else rest

    return EpisodeInfo(episode=episode, subtitle=subtitle or None)


def format_title(novel_title: str, info: EpisodeInfo) -> str:
    """Build the Title tag value for one chapter.

    Args:
        novel_title: The audiobook's title, used as a fallback when no
            subtitle was extracted.
        info: The chapter's extracted episode/subtitle.

    Returns:
        `"Ep {n} - {subtitle}"`, or `"{novel_title} - Ep {n}"` when
        `info.subtitle` is `None`.
    """
    if info.subtitle:
        return f"Ep {info.episode} - {info.subtitle}"
    return f"{novel_title} - Ep {info.episode}"


def build_destination_path(
    dest_root: Path, metadata: AudiobookMetadata, info: EpisodeInfo, suffix: str
) -> Path:
    """Build the final `{dest_root}/{author}/{novel_title}/...` path.

    Args:
        dest_root: Configured audiobook destination root
            (`Settings.audiobooks_dest_dir`).
        metadata: Author/novel_title for this channel.
        info: The chapter's extracted episode/subtitle.
        suffix: File extension to use, including the leading dot.

    Returns:
        The sanitized, collision-free-candidate destination path (final
        collision handling is the caller's responsibility, matching the
        existing `dedup_suffixed_path` convention used elsewhere).
    """
    author_dir = sanitize_filename(metadata.author, fallback_stem="Unknown Author")
    novel_dir = sanitize_filename(metadata.novel_title, fallback_stem="Unknown Title")

    if info.subtitle:
        base_name = f"{metadata.novel_title} - Ep {info.episode:04d} - {info.subtitle}{suffix}"
    else:
        base_name = f"{metadata.novel_title} - Ep {info.episode:04d}{suffix}"
    filename = sanitize_filename(base_name)

    return dest_root / author_dir / novel_dir / filename


def _tag_mp3(path: Path, *, artist: str, album: str, title: str, episode: int) -> None:
    """Write ID3 tags to an `.mp3` file via `mutagen.easyid3.EasyID3`."""
    # mutagen ships a py.typed marker but EasyID3/MP4's own constructors and
    # save() are themselves untyped, so mypy --strict can't see through
    # them; this is a gap in mutagen's stubs, not our own typing.
    try:
        audio = EasyID3(path)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        audio = EasyID3()  # type: ignore[no-untyped-call]

    audio["artist"] = [artist]
    audio["albumartist"] = [artist]
    audio["album"] = [album]
    audio["title"] = [title]
    audio["tracknumber"] = [str(episode)]
    audio.save(path)


def _tag_mp4(path: Path, *, artist: str, album: str, title: str, episode: int) -> None:
    """Write iTunes-style atoms to an `.m4b`/`.m4a` file via `mutagen.mp4.MP4`."""
    audio = MP4(path)  # type: ignore[no-untyped-call]
    audio["\xa9ART"] = [artist]
    audio["aART"] = [artist]
    audio["\xa9alb"] = [album]
    audio["\xa9nam"] = [title]
    audio["trkn"] = [(episode, 0)]
    audio.save()  # type: ignore[no-untyped-call]


def tag_audio_file(
    path: Path, *, author: str, novel_title: str, episode: int, title: str
) -> None:
    """Embed Artist/AlbumArtist/Album/Title/Track tags into `path`.

    Args:
        path: Audio file to tag in place. Must end in `.mp3`, `.m4b`, or
            `.m4a`.
        author: Tagged as Artist and AlbumArtist.
        novel_title: Tagged as Album.
        episode: Tagged as Track.
        title: Tagged as Title.

    Raises:
        ValueError: If `path`'s extension isn't a supported audio format.
    """
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        _tag_mp3(path, artist=author, album=novel_title, title=title, episode=episode)
    elif suffix in {".m4b", ".m4a"}:
        _tag_mp4(path, artist=author, album=novel_title, title=title, episode=episode)
    else:
        raise ValueError(f"Unsupported audiobook extension for tagging: {suffix}")


def _tag_and_move(source: Path, destination: Path, metadata: AudiobookMetadata, info: EpisodeInfo) -> None:
    """Synchronous worker for `process_audiobook_file` (run via `asyncio.to_thread`)."""
    title = format_title(metadata.novel_title, info)
    tag_audio_file(
        source, author=metadata.author, novel_title=metadata.novel_title,
        episode=info.episode, title=title,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move (not Path.replace/os.rename): the destination root may be
    # on a different filesystem/network mount than the download staging
    # area, and os.rename raises OSError(EXDEV) across devices. shutil.move
    # falls back to a copy + delete in that case.
    shutil.move(str(source), str(destination))


async def process_audiobook_file(
    file_path: Path,
    message_id: int,
    metadata: AudiobookMetadata,
    dest_root: Path,
) -> Path:
    """Tag and relocate one downloaded audiobook chapter.

    Called only for channels with `audiobook_mode: true`, after the
    chapter's atomic `.tmp` -> final rename has already completed
    (CLAUDE.md Section 2.5 — the file is durably on disk before this runs).

    Args:
        file_path: Current on-disk location of the freshly-downloaded
            chapter file (its filename is used for episode/subtitle
            extraction; not required to be sanitized already).
        message_id: Telegram message ID, used as the episode-number
            fallback when no `Ep <n>` pattern is found in the filename.
        metadata: Author/novel_title for this channel.
        dest_root: Configured audiobook destination root.

    Returns:
        The file's new, final path after tagging and relocation.

    Raises:
        ValueError: If `file_path`'s extension isn't a supported audio
            format for tagging.
    """
    if file_path.suffix.lower() not in _TAGGABLE_EXTENSIONS:
        raise ValueError(f"Unsupported audiobook extension for tagging: {file_path.suffix}")

    info = extract_episode_info(file_path.name, fallback_episode=message_id)
    destination = dedup_suffixed_path(
        build_destination_path(dest_root, metadata, info, file_path.suffix)
    )

    logger.info(
        "Audiobook processing: %s -> %s (episode=%d, subtitle=%r)",
        file_path, destination, info.episode, info.subtitle,
    )
    await asyncio.to_thread(_tag_and_move, file_path, destination, metadata, info)
    return destination
