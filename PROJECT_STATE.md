# Project State & Developer Guide

**Purpose of this file**: a snapshot of what this Python project actually
does and how it's built, written so a future port to C# (the stated
next-game-plan, for a lighter-weight distributable) can reproduce the same
behavior without re-deriving it from scratch. This is documentation only —
no porting work has started.

Last updated: 2026-08-01. Verify against the code before relying on any
claim here — this file describes a point in time, not a live contract.

---

## 1. What this project is

A CLI tool that bulk-downloads media from Telegram channels/chats and
(newer) bulk-uploads local files back to Telegram, built on
[Telethon](https://docs.telethon.dev/) (MTProto client library) with a
`rich`-based terminal UI. Single `asyncio` event loop, SQLite for durable
state, YAML + `.env` for configuration.

Two run modes, one process: `python -m src.main --mode download` (default)
or `--mode upload`.

## 2. Status: what's implemented vs. not

Implemented and working (all covered by `mypy --strict` + pytest):
- Config-driven multi-channel download with per-channel media-type filter,
  min-date filtering, and `audiobook_mode` tag/relocate post-processing.
- Resumable SQLite state (`chat_progress`, `downloaded_files`,
  `uploaded_files` tables) — safe to kill and restart at any point.
- Atomic downloads: `.tmp` → `os.replace` → state-record, in that order,
  never any other order.
- Bounded concurrency via `asyncio.Semaphore`; `FloodWaitError` handled by
  sleeping the exact server-requested duration + a fixed buffer, capped
  retries, never a tight loop.
- Filename sanitization centralized in one function, dedup-safe (no
  overwrite on collision).
- Bidirectional: `upload_document` (single file) and `upload_media_group`
  (up to 10 files per Telegram album, "API shielding" against rate limits)
  with the same FloodWait policy as downloads.
- Multi-job upload routing: `upload_jobs` config list, each mapping a
  `source_dir` (optionally recursive) to a `target_chat`; upload dedup via
  a fast filename+size+first-1MiB-hash key, scoped per target chat.
- Docker: `Dockerfile`, `docker-compose.yml`, `.dockerignore`.

Known gaps:
- No cross-run resume for *uploads* beyond the dedup-key check (no partial
  in-flight-batch resume — if the process dies mid-batch, files in that
  batch that didn't get `mark_file_uploaded`d will simply be retried next
  run, which is safe but not "resumed" in a finer-grained sense).
- No `.m4b` concatenation for audiobooks (chapters stay individual files by
  design, not an oversight).

## 3. Architecture

```
src/
├── main.py                 Entry point. Owns the asyncio event loop and the
│                            one Settings construction (CLAUDE.md rules).
│                            Parses --mode, wires everything together.
├── config/settings.py       Pydantic models: Settings (.env) + ChannelsFile
│                            (channels.yaml: channels[], upload_jobs[]).
├── core/
│   ├── client.py            Telethon client construction, login flow,
│   │                        resolve_entity (handles invite links),
│   │                        upload_document, upload_media_group.
│   └── exceptions.py        DownloaderError, AuthenticationError,
│                             DownloadFailedError.
├── downloader/
│   ├── worker.py             DownloadManager: per-channel scan loop,
│   │                          semaphore-bounded download tasks, atomic
│   │                          .tmp->final rename, FloodWait/backoff retry.
│   ├── filenames.py           sanitize_filename, dedup_suffixed_path.
│   │                          THE ONLY place filenames touch the filesystem
│   │                          without going through this first.
│   ├── dedup.py               message_dedup_key, hash_file (full SHA-256,
│   │                          used post-download for cross-message dedup).
│   └── audiobook_processor.py Episode/subtitle regex extraction, ID3/MP4
│                              tagging via mutagen, shutil.move relocation.
├── uploader/
│   ├── worker.py              UploaderWorker: multi-job directory scan,
│   │                          media-group batching (<=10 files/batch),
│   │                          3s inter-batch pause, dedup check/record.
│   └── dedup.py                compute_dedup_key: name+size+sha256(first
│                               1MiB) — deliberately NOT a full-file hash.
├── storage/state.py           StateStore: single SQLite connection behind
│                              an asyncio.Lock. chat_progress,
│                              downloaded_files, uploaded_files tables.
└── ui/
    ├── dashboard.py            rich Live dashboard for download mode.
    ├── upload_dashboard.py     rich Live dashboard for upload mode.
    └── logging_config.py       RotatingFileHandler only, no stdout handler.
```

Dependency direction (enforced, checked by review — see `CLAUDE.md`):
`ui` → `downloader`/`uploader`/`storage`/`core` → `config`. Nothing below
`ui/` imports from it; progress reporting crosses that boundary via plain
`Protocol` callback interfaces (`ProgressReporter`,
`UploadProgressReporter`), not direct imports.

Rough size (as of last count): ~2,200 lines across `src/`, largest file
`downloader/worker.py` at 385 lines. No file near the project's own
400-line split threshold except that one, which is borderline by design
(it's the most state-machine-heavy module).

## 4. Data model (SQLite, `data/state.db`)

```sql
chat_progress(chat_id PK, last_message_id, updated_at)
downloaded_files(chat_id, message_id, file_path, content_hash, downloaded_at,
                 PRIMARY KEY(chat_id, message_id))
  INDEX on content_hash
uploaded_files(target_chat, dedup_key, file_path, uploaded_at,
               PRIMARY KEY(target_chat, dedup_key))
```

All three tables share one connection (`WAL` journal mode) serialized by a
single `asyncio.Lock` in `StateStore` — never multiple connections/threads
writing concurrently. A C# port should preserve this "single writer,
explicit serialization" model (e.g. a single `SqliteConnection` behind a
`SemaphoreSlim(1,1)`, or a dedicated writer channel/actor) rather than
relying on SQLite's own locking, to keep the same crash-safety guarantees.

## 5. Key algorithms / invariants worth preserving exactly

These are the parts most likely to introduce subtle bugs if re-implemented
from memory rather than read from source. Read the named function before
porting it.

- **Atomic write pattern** (`downloader/worker.py::_download_one`): write to
  `<final>.tmp`, then `Path.replace()` (atomic on POSIX *and* Windows) to
  the final name, then and only then record state. On any failure, delete
  the `.tmp`; on cancellation, *leave* the `.tmp` for resume — never rename
  a partial file.
- **FloodWaitError policy** (`core/client.py`, `downloader/worker.py`):
  sleep for `server_seconds + fixed_buffer` (2.0s), never a growing
  multiple, capped at `_MAX_FLOOD_WAIT_RETRIES` (5) attempts. This exact
  shape (not exponential backoff) is deliberate — see the comments in both
  files for why.
- **Anti-ban pacing** (`downloader/worker.py`): fixed device fingerprint on
  every connection (`core/client.py`'s `_DEVICE_MODEL` etc. — a realistic,
  *unchanging* signature, not randomized per run) + a randomized 2–5s delay
  between downloads per worker slot. A C# port using a different MTProto
  library (see §7) should replicate both: a stable client init string and
  randomized inter-request pacing, not just the flood-wait reaction.
- **Filename sanitization** (`downloader/filenames.py::sanitize_filename`):
  strips Windows-illegal chars even on POSIX (portability), rejects
  reserved device names (`CON`, `COM1`...), truncates to 255 UTF-8 bytes
  without splitting a multi-byte char, strips path traversal by taking only
  the final path segment via both `PureWindowsPath` and `PurePosixPath`.
  Every filename derived from remote/Telegram-controlled data goes through
  this one function — no ad-hoc sanitization anywhere else.
- **Dedup**: two distinct schemes, don't conflate them.
  - Download identity dedup: `(chat_id, message_id)` — cheap, checked
    *before* downloading.
  - Download content dedup: full SHA-256 of the completed file
    (`downloader/dedup.py::hash_file`) — computed *after* download, used
    for cross-message duplicate detection via `find_by_content_hash`.
  - Upload dedup: `uploader/dedup.py::compute_dedup_key` — filename + size +
    SHA-256 of only the *first 1 MiB*, computed *before* upload (fast,
    approximate — documented trade-off, not a bug).
- **Media group batching** (`uploader/worker.py::process_queue`): pending
  (not-yet-uploaded) files are grouped by contiguous target chat (queue is
  naturally job-contiguous from `build_queue`), then chunked to
  `MEDIA_GROUP_MAX_SIZE` (10, Telegram's real album limit — not a tunable).
  A batch never spans two target chats. 3s `asyncio.sleep` between batches,
  not after the last one.
- **Recursive vs. non-recursive scan** (`uploader/worker.py::build_queue`):
  `Path.rglob("*")` vs `Path.iterdir()`, filtered to `is_file()`, sorted for
  deterministic order. Missing `source_dir` yields zero items for that job,
  not an error (normal "nothing to upload yet" state).
- **Audiobook tagging** (`downloader/audiobook_processor.py`): regex-based
  episode/subtitle extraction with a specific "trailing uploader tag"
  peel-off rule (see the two regex comments — the space-before-hyphen
  distinction is load-bearing, don't simplify it away). `shutil.move`, not
  `os.rename`, specifically to survive cross-filesystem moves (`EXDEV`).

## 6. External dependencies and their likely C# counterparts

| Python (this repo) | Role | Likely C# equivalent |
|---|---|---|
| `telethon` | MTProto client, auth, downloads/uploads, FloodWaitError | [WTelegramClient](https://github.com/wiz0u/WTelegramClient) — closest match (raw MTProto, similar API shape, actively maintained). TDLib bindings are the heavier alternative. |
| `cryptg` | Optional native crypto accel for Telethon | N/A — WTelegramClient uses managed/BouncyCastle crypto; no direct equivalent needed. |
| `pydantic` / `pydantic-settings` | Config validation (`Settings`, `ChannelsFile`, `extra="forbid"` fail-fast) | `System.Text.Json` + manual validation, or `FluentValidation`, or source-generated config binding with strict unknown-key rejection (`.NET`'s default binder is lenient by default — must opt into strict mode to match `extra="forbid"` behavior). |
| `PyYAML` | `channels.yaml` parsing | `YamlDotNet`. |
| `rich` | Live terminal dashboard, progress bars | `Spectre.Console` — very close conceptual match (`Live`, progress columns, panels). |
| `mutagen` | ID3/MP4 tag writing | `TagLibSharp`. |
| `sqlite3` (stdlib) | State store | `Microsoft.Data.Sqlite` or `System.Data.SQLite`, same WAL + single-writer-lock discipline. |
| `asyncio` | Single event loop, semaphores, locks | `async`/`await` + `SemaphoreSlim`; C# has no single-owned-loop concept to replicate — just don't spin up unbounded concurrent tasks (mirror the semaphore-bounding rule). |

**Telethon `.session` file is not portable.** It's a Telethon-specific
SQLite schema. A C# port with a different MTProto library needs its own
first-run interactive login — sessions cannot be carried over. Budget for
that in the port plan; it's not a bug to "fix", just a fact to communicate
to users switching versions.

## 7. Configuration surface (keep schema-compatible if possible)

`config/channels.yaml` / `config/channels.example.yaml` — top-level keys:

```yaml
download_root: downloads
max_concurrent_downloads: 5          # 1-50
channels: [ ChannelConfig, ... ]
upload_jobs: [ UploadJobConfig, ... ]
```

`ChannelConfig`: `id` (int|str), `name`, `media_types` (subset of
`[photo, video, document, audio]`), `output_subdir`, `min_date` (ISO-8601,
see gap noted in §2), `audiobook_mode` (bool), `metadata` (required iff
`audiobook_mode: true`: `author`, `novel_title`).

`UploadJobConfig`: `source_dir`, `target_chat` (int|str), `recursive`
(bool, default false).

`.env` keys: `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `TG_SESSION_NAME`
(default `data/downloader`), `AUDIOBOOKS_DEST_DIR` (default
`downloads/Audiobooks`).

If the C# port reads the *same* `channels.yaml`/`.env` files, users migrate
with zero config changes — strongly recommended over inventing a new
schema. Every field above has a `model_validator`/`ConfigDict(extra=
"forbid")` fail-fast rule in `settings.py`; replicate the "reject unknown
keys" behavior specifically, since that's what catches config typos today
(see the several `test_..._rejects_unknown_field_typo` tests).

## 8. Testing approach (mirror this, don't skip it)

Every module above has a matching file under `tests/`, using **fakes/duck
typing** for Telethon objects rather than a mocking framework or live
network calls (`FakeClient`, `FakeMessage`, `FakeDocumentAttribute`, etc. —
see `tests/downloader/test_worker.py` for the fullest example). Real
`StateStore` instances against `tmp_path` SQLite files, not mocked. This
kept the whole suite at ~2 seconds for 116 tests with zero flakiness from
mocking mismatches. A C# port should use the equivalent (hand-written test
doubles implementing the same interfaces, or `WTelegramClient`'s own
test-friendly seams if it has them) over a heavy mocking framework, for the
same reason.

`mypy --strict` on `src/` (not `tests/`) is a hard gate — the project has
zero `Any` outside two documented `# type: ignore[no-untyped-call]` spots
for `mutagen`'s untyped API. The C# equivalent bar is "nullable reference
types enabled, zero warnings, no `dynamic`."

## 9. Suggested porting order (mirrors how this project was actually built)

Building it in this order kept each stage independently testable and
matches the dependency graph in §3 (build the bottom of the graph first):

1. **Config + state layer**: `Settings`/`ChannelsFile` equivalents, SQLite
   `StateStore` with the same three tables and single-writer discipline.
   Get config-parsing tests (including "reject unknown key") green first —
   everything else depends on this being trustworthy.
2. **Filename sanitization + dedup helpers** (`downloader/filenames.py`,
   `downloader/dedup.py`, `uploader/dedup.py`): pure functions, no network,
   easiest to port 1:1 and unit test in isolation.
3. **MTProto client wrapper** (`core/client.py` equivalent): auth flow,
   `resolve_entity` (including invite-link handling), single-file
   upload/download with the exact FloodWait retry shape from §5. This is
   the highest-risk piece because it depends on WTelegramClient's actual
   API surface, which won't mirror Telethon 1:1 — expect the most
   deviation from the Python source here.
4. **Download worker**: semaphore-bounded scan loop, atomic `.tmp` writes,
   anti-ban pacing. Port `_download_one` and `_download_with_retries`
   near-verbatim in structure even if the Telethon calls they wrap change
   shape.
5. **Upload worker**: media-group batching, multi-job routing, dedup
   check/record — this is the newest, least-baked part of the Python
   version, so treat the Python source as the spec but feel free to
   simplify if the port reveals rough edges (e.g. the synthetic
   comma-joined "filename" used for batch progress reporting in
   `UploadFileProgress` is a known wart, not a contract worth preserving).
6. **UI layer** (`Spectre.Console` dashboards): last, since it's the
   easiest to eyeball-verify and least likely to hide subtle bugs.
7. **Docker**: same three files (`Dockerfile`, `docker-compose.yml`,
   `.dockerignore`), adjusted for a compiled/published C# binary instead of
   a `pip install` layer — likely a smaller final image, which is the
   whole point of the port.

At each stage, port the matching `tests/` file alongside the source file,
not after — the Python test suite is effectively the spec for edge cases
(empty directories, FloodWait exhaustion, collision suffixing, etc.) that
are easy to forget when reading only the implementation.

## 10. Things to explicitly re-verify before porting (don't trust this doc)

This file is a snapshot, not a live contract. Before treating any claim
above as ground truth for the port:
- Re-check `min_date` enforcement status (§2) directly in
  `src/downloader/worker.py`.
- Re-run `git log --oneline` and diff against this file's "last updated"
  commit to see what's changed since.
- Re-run `pytest -q` and `mypy --strict src` to confirm the "clean" claims
  in §8 still hold.
