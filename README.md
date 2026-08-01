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

See `config/channels.example.yaml` for a fuller, annotated set of examples
covering each common use case (plain archive, documents-only, raw audio
with no post-processing, single and multi-book `audiobook_mode`) plus a
field-by-field reference.

Settings are loaded and validated by `src/config/settings.py` using
`pydantic-settings`: `.env` supplies secrets/runtime knobs, the YAML file
supplies the channel list, and both are merged into one immutable `Settings`
object at startup.

> **Known gap:** `ChannelConfig` also accepts an optional `min_date` field,
> but the downloader doesn't enforce it yet — no messages are currently
> filtered by date. Don't rely on it until this is implemented.

**`audiobook_mode` channels** are post-processed by
`src/downloader/audiobook_processor.py` immediately after each chapter's
atomic download completes: the episode number and subtitle are parsed from
the filename (falling back to the Telegram message ID), ID3/MP4 tags
(Artist, Album, Title, Track) are embedded via `mutagen`, and the file is
moved — via `shutil.move`, safe across filesystem boundaries — into
`{AUDIOBOOKS_DEST_DIR}/{author}/{novel_title}/`. `audiobook_mode: true`
requires a `metadata` block (`author` + `novel_title`); config loading fails
fast if it's missing. Chapters are kept as individual files — there is no
`ffmpeg` dependency or `.m4b` concatenation step.

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
│   │   └── audiobook_processor.py   # audiobook_mode tagging + relocation
│   ├── storage/
│   │   ├── __init__.py
│   │   └── state.py            # SQLite schema + last-message-id tracking
│   └── ui/
│       ├── __init__.py
│       ├── dashboard.py        # rich Live dashboard (progress, throughput, logs)
│       └── logging_config.py   # Rotating file handler setup (no stdout handler)
└── tests/
    ├── config/
    ├── core/
    ├── downloader/
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
python -m src.main
```

On first run you'll be prompted (via the `rich`-rendered UI) for the Telegram
login code; after that, `data/downloader.session` keeps you logged in.

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
mypy --strict src
pytest
```

All contributions must satisfy the rules in `CLAUDE.md` before being merged.
