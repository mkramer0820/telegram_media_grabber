# Telegram Batch Media Downloader

A modern, modular, production-grade tool for bulk-downloading media (photos,
videos, documents) from one or more Telegram channels/chats, built on
[Telethon](https://docs.telethon.dev/) and [rich](https://rich.readthedocs.io/).

Rebuilt from scratch — domain-driven layout, async workers, persistent SQLite
state, and a live terminal dashboard. See `CLAUDE.md` for the non-negotiable
engineering rules this codebase follows.

---

## Features

- **Config-driven targets.** Channels/chats to scan are declared in a YAML (or
  JSON) config file — no hardcoded chat lists. Each entry can specify its own
  media-type filter, output subfolder, and date range.
- **Persistent Telethon session.** Auth happens once; a `.session` file
  (Telethon's SQLite-backed session) is reused on every run so you are not
  re-prompted for a login code.
- **Resumable scanning via SQLite state.** The last successfully processed
  message ID per chat is recorded in a local SQLite database. Re-running the
  tool never rescans messages it has already handled, and a crash mid-run
  loses at most the in-flight batch.
- **Graceful `FloodWaitError` handling.** Telegram rate limits are respected:
  the tool sleeps for exactly the duration Telegram requests, shows the wait
  in the live UI, and resumes automatically — it never hammers the API in a
  retry loop.
- **Bounded async concurrency.** Downloads run concurrently across chats and
  messages, capped by a configurable semaphore, so the tool is fast without
  tripping Telegram's abuse detection.
- **Atomic, deduplicated file writes.** Every file is written to a `.part`
  path and atomically renamed on completion. Duplicate media (by chat+message
  identity, and optionally content hash) is detected and never silently
  overwrites existing files.
- **Live terminal dashboard.** A `rich`-based UI shows per-chat progress bars,
  overall throughput, current flood-wait countdowns, and a scrolling log of
  recently downloaded files — without ever touching `print()`.
- **Rotating file logs.** All diagnostic/debug logging is written to
  `logs/app.log` (rotated by size), completely separate from the interactive
  terminal UI.

---

## Requirements

- Python 3.11+
- A Telegram API ID/hash from https://my.telegram.org
- Windows, macOS, or Linux

---

## Configuration

### `.env` (secrets — never committed)

```dotenv
TG_API_ID=123456
TG_API_HASH=your_api_hash_here
TG_PHONE=+15551234567
TG_SESSION_NAME=data/downloader

# Destination root for audiobook_mode channels (see below). Defaults to
# downloads/Audiobooks; override with an absolute external/NAS path.
AUDIOBOOKS_DEST_DIR=downloads/Audiobooks
```

### `config/channels.yaml` (download targets)

```yaml
download_root: downloads
max_concurrent_downloads: 5
channels:
  - id: "@some_public_channel"
    name: photos_channel
    media_types: [photo, video]
    output_subdir: photos_channel
  - id: -1001234567890
    name: private_chat_export
    media_types: [document]
    output_subdir: docs
  - id: -1001987654321
    name: shadow_slave_audiobook
    media_types: [audio, document]
    output_subdir: shadow_slave_staging
    audiobook_mode: true
    metadata:
      author: "Guiltythree"
      novel_title: "Shadow Slave"
```

`upload_jobs` configures upload mode (see "Running" below): a list of
`{source_dir, target_chat, recursive}` entries, each routing one local
directory (optionally scanned recursively) to one destination chat. Files
within a job are sent in batches of up to 10 as Telegram media groups
(albums), with a pause between batches to stay within API rate limits.

```yaml
upload_jobs:
  - source_dir: uploads/photos_channel
    target_chat: "@some_public_channel"
    recursive: false
  - source_dir: uploads/docs_archive
    target_chat: -1001234567890
    recursive: true
```

See `config/channels.example.yaml` for a fuller, annotated set of examples
covering each common use case (plain archive, documents-only, raw audio
with no post-processing, single and multi-book `audiobook_mode`) plus a
field-by-field reference.

Settings are loaded and validated by `src/config/settings.py` using
`pydantic-settings`: `.env` supplies secrets/runtime knobs, the YAML file
supplies the channel list, and both are merged into one immutable `Settings`
object at startup.

`ChannelConfig.min_date` (ISO-8601) is enforced: messages are scanned
newest-first, and scanning stops for a channel once a message older than
`min_date` is reached.

**`audiobook_mode` channels** are post-processed by
`src/downloader/audiobook_processor.py` immediately after each chapter's
atomic download completes: the episode number and subtitle are parsed from
the filename — either "Ep &lt;n&gt; - &lt;subtitle&gt;", or a cleanly-delimited bare
number/range anywhere in the filename (leading, trailing, or the whole
stem) like "1114", "5-6", "Shadow Slave 1751-1846" (trailing range), or
"0001_0100_Weakest_Beast_Tamer" (leading range, "_" separator; a range
uses its start number) — never from the Telegram message ID. If the
filename has no parsable number at all, the next
episode number is inferred from the highest "Ep n" already in the
destination directory, plus one. ID3/MP4 tags
(Artist, Album, Title, Track) are embedded via `mutagen`, and the file is
moved — via `shutil.move`, safe across filesystem boundaries — into
`{AUDIOBOOKS_DEST_DIR}/{author}/{novel_title}/`. `audiobook_mode: true`
requires a `metadata` block (`author` + `novel_title`); config loading fails
fast if it's missing. Chapters are kept as individual files — there is no
`ffmpeg` dependency or `.m4b` concatenation step.

If a channel was downloaded with `audiobook_mode` off (or before this
episode-number logic existed) and files are stuck in staging or tagged with
the wrong number, see `--mode reprocess` and `--mode verify` under
"Running" below.

---

## Directory Structure

```
telegram_media_grabber/
├── CLAUDE.md                  # Engineering rules (read first)
├── README.md                  # This file
├── requirements.txt
├── .env                       # Local secrets (gitignored)
├── config/
│   └── channels.yaml           # Declarative list of channels/chats to grab
├── data/                       # Canonical home for all persistent local state (gitignored)
│   ├── downloader.session      # Telethon session file
│   └── state.db                # SQLite state database (message tracking + dedup)
├── logs/
│   └── app.log                 # Rotating backend log (never printed to terminal)
├── downloads/                  # Default media output root (gitignored)
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entry point: wires config, state, core, ui together
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py         # Pydantic Settings: .env + channels.yaml
│   ├── core/
│   │   ├── __init__.py
│   │   ├── client.py           # Telethon client construction & auth/login flow
│   │   └── exceptions.py       # Domain-specific exception types
│   ├── downloader/
│   │   ├── __init__.py
│   │   ├── worker.py                # Async download workers, semaphore-bounded
│   │   ├── filenames.py             # Centralized filename sanitization
│   │   ├── dedup.py                 # Dedup key computation / collision handling
│   │   ├── audiobook_processor.py   # audiobook_mode tagging + relocation
│   │   ├── reprocessor.py           # --mode reprocess: fixes files stuck in staging
│   │   └── episode_verifier.py      # --mode verify: re-checks episode numbers vs Telegram
│   ├── uploader/
│   │   ├── __init__.py
│   │   └── worker.py            # UploaderWorker: scans a dir, uploads to a chat
│   ├── storage/
│   │   ├── __init__.py
│   │   └── state.py            # SQLite schema + last-message-id tracking
│   └── ui/
│       ├── __init__.py
│       ├── dashboard.py        # rich Live dashboard for download mode
│       ├── upload_dashboard.py # rich Live dashboard for upload mode
│       └── logging_config.py   # Rotating file handler setup (no stdout handler)
├── uploads/                    # Default upload source dir (gitignored)
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
└── tests/
    ├── config/
    ├── core/
    ├── downloader/
    ├── uploader/
    ├── storage/
    └── ui/
```

**Dependency direction:** `ui` → `downloader`/`storage`/`core` → `config`.
Nothing below `ui/` imports from it. `main.py` is the only place that
constructs the event loop and wires all layers together.

**Persistent state lives under `data/`.** The Telethon `.session` file
(`TG_SESSION_NAME`, default `data/downloader`) and the SQLite state database
(`state_db_path`, default `data/state.db`) both default into this single
directory, so backing up or wiping "everything Telegram/local-state related"
is always a matter of that one folder. `logs/` and `downloads/` remain
separate since they aren't state Telethon/SQLite depend on to function.

---

## State Tracking (SQLite)

A single SQLite database (`data/state.db` by default) tracks, per chat:

| column              | type    | meaning                                      |
|---------------------|---------|-----------------------------------------------|
| `chat_id`           | INTEGER | Telegram chat/channel ID (primary key)        |
| `last_message_id`   | INTEGER | Highest message ID fully processed             |
| `updated_at`        | TEXT    | ISO-8601 timestamp of last update              |

and, per downloaded file:

| column          | type    | meaning                                    |
|-----------------|---------|---------------------------------------------|
| `chat_id`       | INTEGER | Owning chat                                  |
| `message_id`    | INTEGER | Source message ID                            |
| `file_path`     | TEXT    | Final on-disk path                           |
| `content_hash`  | TEXT    | Optional hash for cross-message dedup        |
| `downloaded_at` | TEXT    | ISO-8601 timestamp                           |

`(chat_id, message_id)` is the dedup primary key, enforced with a `UNIQUE`
constraint so re-scans are naturally idempotent.

---

## Running

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in TG_API_ID / TG_API_HASH / TG_PHONE
python -m src.main                    # download mode (default)
python -m src.main --mode upload      # upload mode
python -m src.main --mode reprocess   # fix audiobook files stuck in staging
python -m src.main --mode verify      # re-check tagged episode numbers against Telegram
```

On first run you'll be prompted (via the `rich`-rendered UI) for the Telegram
login code; after that, `data/downloader.session` keeps you logged in.

Upload mode requires at least one entry in `upload_jobs` (see
"Configuration" above); it scans each job's `source_dir` and sends its
files to that job's `target_chat`, batched into media groups of up to 10
files with a pause between batches to stay within Telegram's rate limits.

Reprocess mode is fully offline (no Telegram connection): for every
`audiobook_mode` channel, it scans `download_root/{output_subdir}` for
files that were downloaded but never tagged and moved into
`AUDIOBOOKS_DEST_DIR` (e.g. because `audiobook_mode` was turned on after
they were downloaded, or the files predate this app's state tracking
entirely), tags and relocates them, and corrects their `downloaded_files`
state record where one exists — files with no matching record are still
tagged/moved, just with nothing to correct. Safe to run repeatedly — once
a file is moved out of staging there's nothing left for it to find.

Verify mode is online (one batched `get_messages` request per channel): for
every `audiobook_mode` channel, it re-fetches each already-tagged file's
source message and re-derives its episode number from Telegram's raw
document filename via `extract_episode_info`. If that disagrees with what's
currently on disk — e.g. a file tagged before bare-numeric-filename support
existed, back when the only fallback was Telegram's message ID — it re-tags
and relocates the file to its true episode number and corrects the state
record. Safe to run repeatedly; files that are already correct are left
untouched.

---

## Running with Docker

```bash
cp .env.example .env          # fill in TG_API_ID / TG_API_HASH / TG_PHONE
cp config/channels.example.yaml config/channels.yaml   # then edit it
docker compose up -d
```

This builds the image, mounts `./config`, `./data`, `./logs`, `./downloads`,
and `./uploads` into the container so state/output survive restarts, and
starts the app in download mode. To run upload mode instead, override the
command:

```bash
docker compose run --rm telegram-media-grabber --mode upload
```

The first run needs the interactive Telegram login — attach to the
container's logs to enter the code:

```bash
docker compose logs -f
```

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
mypy --strict src
pytest
```

All contributions must satisfy the rules in `CLAUDE.md` before being merged.
