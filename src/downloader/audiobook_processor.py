"""Post-processing for `audiobook_mode` channels: tag, rename, and relocate.

Invoked by `src.downloader.worker` immediately after a chapter file's atomic
`.tmp` -> final rename succeeds (CLAUDE.md Section 2.1/2.5 still apply up to
that point). From there this module:

  1. Extracts an episode/chapter number (and optional subtitle) from the raw
     filename: either "Ep <n> - <subtitle>" or a bare number/range like
     "1114" or "5-6". Author/novel_title always come from config
     (`AudiobookMetadata`), never from the filename.
  2. If the filename carries no parsable number at all, infers the next
     episode number from what's already in the destination directory
     (highest existing "Ep <n>" + 1) — never from the Telegram message ID,
     which is an arbitrary ID shared across the whole chat and unrelated to
     the show's own episode count.
  3. Embeds Artist/AlbumArtist/Album/Title/Track tags via `mutagen`.
  4. Moves the file into `{dest_root}/{author}/{novel_title}/...` using
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

# Some channels post chapters named as just a number, or a number range for
# a bundled multi-chapter file — and that number/range can sit anywhere in
# the filename, not only at the end: "1114.m4a", "1114..m4a" (a trailing
# dot survives in Path(...).stem when the raw filename itself has a double
# dot), "5-6.m4a", "Shadow Slave 1751-1846.m4a" (title prefix, trailing
# range), or "0001_0100_Weakest_Beast_Tamer.mp3" (leading range, title
# suffix — "_" as the range separator here, not "-"). The number/range
# must be cleanly delimited — bounded by the start/end of the stem or a
# whitespace/underscore/hyphen/dot separator on each side — so a token
# that's merely adjacent to other non-digit text without a separator
# doesn't get misread as a number.
_NUMBER_TOKEN_PATTERN = re.compile(r"(?:^|[\s_-])(?P<start>\d+)(?:[-_](?P<end>\d+))?(?=$|[\s_.-])")

# Scans an already-tagged destination filename (e.g. "Shadow Slave - Ep 0009
# - Title.mp3") for its episode number, used only by
# infer_next_episode_number's directory scan.
_EXISTING_EPISODE_TAG_PATTERN = re.compile(r"\bEp\s*(?P<episode>\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EpisodeInfo:
    """A chapter's episode number and optional subtitle."""

    episode: int
    subtitle: str | None


def extract_episode_info(raw_filename: str) -> EpisodeInfo | None:
    """Extract an episode number and subtitle from a raw Telegram filename.

    Tries two patterns, in order:
      1. "Ep <n> - <subtitle>" (or "Episode"/"ep."/colon separator) anywhere
         in the filename stem.
      2. A cleanly-delimited bare number or number range anywhere in the
         stem — leading, trailing, or the whole stem — e.g. "1114", "5-6",
         "Shadow Slave 1751-1846" (trailing range), or
         "0001_0100_Weakest_Beast_Tamer" (leading range). A range uses its
         start number. Author/novel_title always come from config
         regardless of what other text surrounds the number here.

    Args:
        raw_filename: The original filename (with or without extension),
            e.g. "__Shadow Slave.Ep 2027 - The Strength of the Wolf-XtreamStories.mp3"
            or "1114.m4a".

    Returns:
        An `EpisodeInfo` if either pattern matched, otherwise `None` — the
        caller decides what to do when no episode number is present in the
        filename at all (see `infer_next_episode_number`).
    """
    stem = Path(raw_filename).stem

    match = _EPISODE_PATTERN.search(stem)
    if match is not None:
        episode = int(match.group("episode"))
        rest = match.group("rest").strip()
        tag_match = _TRAILING_UPLOADER_TAG_PATTERN.match(rest)
        subtitle = tag_match.group("subtitle").strip() if tag_match else rest
        return EpisodeInfo(episode=episode, subtitle=subtitle or None)

    numeric_match = _NUMBER_TOKEN_PATTERN.search(stem)
    if numeric_match is not None:
        return EpisodeInfo(episode=int(numeric_match.group("start")), subtitle=None)

    return None


def parse_tagged_episode_number(path: Path) -> int | None:
    """Return the episode number already encoded in a tagged filename, if any.

    Looks for this app's own "... Ep <n> ..." naming (see
    `build_destination_path`) in `path`'s stem — used both by
    `infer_next_episode_number`'s directory scan and by
    `src.downloader.episode_verifier` to compare a file's currently-tagged
    number against the truth re-derived from Telegram.

    Args:
        path: A file possibly already named by this app.

    Returns:
        The parsed episode number, or `None` if the filename doesn't
        contain a recognizable "Ep <n>" tag.
    """
    match = _EXISTING_EPISODE_TAG_PATTERN.search(path.stem)
    return int(match.group("episode")) if match is not None else None


def infer_next_episode_number(dest_dir: Path) -> int:
    """Infer the next episode number from files already tagged in `dest_dir`.

    Last-resort fallback for a chapter whose filename carries no parsable
    episode number at all (`extract_episode_info` returned `None`). Scans
    `dest_dir` for files this app has already named "... Ep <n> ..." and
    returns one past the highest number found — a continuation of what's
    actually on disk for this book, not an arbitrary Telegram message ID.

    Args:
        dest_dir: The book's destination directory
            (`dest_root/{author}/{novel_title}`).

    Returns:
        `max(existing episode numbers) + 1`, or `1` if `dest_dir` doesn't
        exist yet or has no recognizably-tagged files.
    """
    if not dest_dir.exists():
        return 1

    numbers = [
        number
        for path in dest_dir.iterdir()
        if path.is_file()
        for number in [parse_tagged_episode_number(path)]
        if number is not None
    ]
    return max(numbers) + 1 if numbers else 1


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


def book_dir(dest_root: Path, metadata: AudiobookMetadata) -> Path:
    """Return the book's destination directory: `dest_root/{author}/{novel_title}`.

    Shared by `build_destination_path` and `infer_next_episode_number`'s
    caller so both agree on exactly where a book's files live.

    Args:
        dest_root: Configured audiobook destination root.
        metadata: Author/novel_title for this channel.

    Returns:
        The sanitized `dest_root/{author}/{novel_title}` directory path.
    """
    author_dir = sanitize_filename(metadata.author, fallback_stem="Unknown Author")
    novel_dir = sanitize_filename(metadata.novel_title, fallback_stem="Unknown Title")
    return dest_root / author_dir / novel_dir


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
    if info.subtitle:
        base_name = f"{metadata.novel_title} - Ep {info.episode:04d} - {info.subtitle}{suffix}"
    else:
        base_name = f"{metadata.novel_title} - Ep {info.episode:04d}{suffix}"
    filename = sanitize_filename(base_name)

    return book_dir(dest_root, metadata) / filename


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


async def apply_episode_tagging(
    file_path: Path, info: EpisodeInfo, metadata: AudiobookMetadata, dest_root: Path
) -> Path:
    """Tag `file_path` in place and move it to the destination for `info`.

    Unlike `process_audiobook_file`, this never derives `info` from
    `file_path`'s own name — callers pass it explicitly. This is what makes
    it safe for `src.downloader.episode_verifier` to use: correcting a
    previously mistagged file means the file's *current* name already has
    the wrong "Ep <n>" baked in (which `extract_episode_info` would happily
    re-match), so the caller must supply the freshly-verified `info` instead.

    Args:
        file_path: Current on-disk location of the audio file to tag/move.
        info: The episode identity to apply.
        metadata: Author/novel_title for this channel.
        dest_root: Configured audiobook destination root.

    Returns:
        The file's new path after tagging and relocation (dedup-suffixed
        if the natural destination name is already taken by a different
        file — CLAUDE.md Section 2.4: never overwrite).
    """
    destination = dedup_suffixed_path(
        build_destination_path(dest_root, metadata, info, file_path.suffix)
    )
    await asyncio.to_thread(_tag_and_move, file_path, destination, metadata, info)
    return destination


async def process_audiobook_file(
    file_path: Path,
    message_id: int | None,
    metadata: AudiobookMetadata,
    dest_root: Path,
) -> Path:
    """Tag and relocate one downloaded audiobook chapter.

    Called for channels with `audiobook_mode: true`, normally right after
    the chapter's atomic `.tmp` -> final rename completes (CLAUDE.md
    Section 2.5 — the file is durably on disk before this runs). Also used
    by `--mode reprocess` for files that predate this app's state tracking
    (grabbed before `downloaded_files` existed, or before `audiobook_mode`
    was turned on) — such a file has no known message ID, hence `None`.

    Args:
        file_path: Current on-disk location of the freshly-downloaded
            chapter file (its filename is used for episode/subtitle
            extraction; not required to be sanitized already).
        message_id: Telegram message ID, or `None` if unknown (no matching
            `downloaded_files` record). Only used for logging traceability
            back to the source message — never as the episode number (see
            `extract_episode_info`/`infer_next_episode_number`).
        metadata: Author/novel_title for this channel (always from config,
            never guessed from the filename).
        dest_root: Configured audiobook destination root.

    Returns:
        The file's new, final path after tagging and relocation.

    Raises:
        ValueError: If `file_path`'s extension isn't a supported audio
            format for tagging.
    """
    if file_path.suffix.lower() not in _TAGGABLE_EXTENSIONS:
        raise ValueError(f"Unsupported audiobook extension for tagging: {file_path.suffix}")

    info = extract_episode_info(file_path.name)
    if info is None:
        next_episode = infer_next_episode_number(book_dir(dest_root, metadata))
        info = EpisodeInfo(episode=next_episode, subtitle=None)
        logger.warning(
            "No episode number in filename %r (message_id=%s); inferring "
            "episode %d from the highest existing episode in the "
            "destination directory.",
            file_path.name, message_id, next_episode,
        )

    destination = await apply_episode_tagging(file_path, info, metadata, dest_root)

    logger.info(
        "Audiobook processing: %s -> %s (episode=%d, subtitle=%r, message_id=%s)",
        file_path, destination, info.episode, info.subtitle, message_id,
    )
    return destination
