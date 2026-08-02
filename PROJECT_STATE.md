# Project State & Developer Guide

**Purpose of this file**: a snapshot of what this Python project actually
does and how it's built, written so a future port to C# (the stated
next-game-plan, for a lighter-weight distributable) can reproduce the same
behavior without re-deriving it from scratch. This is documentation only —
no porting work has started.

Last updated: 2026-08-02. Verify against the code before relying on any
claim here — this file describes a point in time, not a live contract.

**Companion document**: `CSHARP_PORT_GUIDE.md` is the actionable
instruction set for actually starting the C# rewrite (architecture, DI,
persistence, testing, distribution, plus the metadata-overrides feature
design) — this file is "what the Python app does," that one is "how to
build the C# version." Read this file first, then that one.

---

## 1. What this project is

A CLI tool that bulk-downloads media from Telegram channels/chats and
(newer) bulk-uploads local files back to Telegram, built on
[Telethon](https://docs.telethon.dev/) (MTProto client library) with a
`rich`-based terminal UI. Single `asyncio` event loop, SQLite for durable
state, YAML + `.env` for configuration.

Four run modes, one process: `python -m src.main --mode download` (default),
`--mode upload`, `--mode reprocess` (offline audiobook staging repair), or
`--mode verify` (online audiobook episode-number re-verification against
Telegram).

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
- Audiobook episode-number extraction from the raw filename itself, tried
  in order: "Ep n" pattern; "Vol n" pattern (a whole bundled book — tagged
  `Vol NN`, 2-digit padding, a completely separate number/label space from
  chapters, never `Ep NNNN`, never colliding with a same-numbered chapter);
  then a cleanly-delimited bare number or range anywhere in the filename
  (leading, trailing, or the whole stem — e.g. "1114", "5-6",
  "Shadow Slave 1751-1846" (trailing range), or
  "0001_0100_Weakest_Beast_Tamer" (leading range, "_" separator); a range
  uses its start number). Never from Telegram's message ID. If the
  filename has no number at all, `infer_next_episode_number` (highest
  existing "Ep n" in the destination directory + 1 — volumes excluded)
  is the fallback; message ID is never part of the number in any path,
  only used for log traceability.
- `--mode reprocess` (offline): finds `audiobook_mode` files that were
  downloaded but never tagged/relocated out of staging — either because
  `audiobook_mode` was enabled after they were already downloaded (dedup
  is keyed on `(chat_id, message_id)`, so such files are never retried by
  a normal download run) or because they predate this app's state
  tracking entirely (no `downloaded_files` row at all) — and fixes them.
  Files with no matching state row are still tagged/relocated; only the
  state-repair step is skipped for them.
- `--mode verify` (online, one batched `get_messages` request per channel):
  re-derives each already-tagged file's true episode number straight from
  Telegram's raw document filename and corrects any mismatch — the
  belt-and-suspenders fix for files mistagged *before* the bare-numeric
  filename support above existed, back when message-ID fallback was the
  only option.

Known gaps:
- No cross-run resume for *uploads* beyond the dedup-key check (no partial
  in-flight-batch resume — if the process dies mid-batch, files in that
  batch that didn't get `mark_file_uploaded`d will simply be retried next
  run, which is safe but not "resumed" in a finer-grained sense).
- No `.m4b` concatenation for audiobooks (chapters stay individual files by
  design, not an oversight).
- `--mode reprocess` and `--mode verify` process files sequentially, not
  concurrently — fine at the scale this project runs at (tens to low
  hundreds of files per channel), but worth knowing if a port adds
  concurrency here without a reason to.

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
│   ├── audiobook_processor.py Episode/subtitle regex extraction, ID3/MP4
│   │                          tagging via mutagen, shutil.move relocation.
│   │                          apply_episode_tagging: tag+move using an
│   │                          EXPLICITLY-given EpisodeInfo (doesn't re-derive
│   │                          from the file's current name) — the primitive
│   │                          both process_audiobook_file and
│   │                          episode_verifier build on.
│   ├── reprocessor.py         --mode reprocess (offline): AudiobookReprocessor.
│   └── episode_verifier.py    --mode verify (online): EpisodeVerifier.
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

Rough size (as of last count): ~3,000 lines across `src/`. Largest files:
`downloader/audiobook_processor.py` (429) and `downloader/worker.py` (391).
**`audiobook_processor.py` now exceeds the project's own ~400-line
CLAUDE.md split threshold** — it grew organically through several rounds
of real-world bug fixes (see §10) rather than being designed up front.
It's still one file in the Python codebase (not yet split — that cleanup
hasn't been done), but a from-scratch C# design should NOT reproduce this
shape; see §10 for the recommended split.

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

No schema change was needed to support `--mode reprocess`/`--mode verify` —
both are state *repair* operations built on three extra `StateStore`
methods over the existing `downloaded_files` table:
`list_downloaded_records(chat_id)`, `find_downloaded_record_by_path(path)`,
and `update_downloaded_file_path(chat_id, message_id, path, hash)`. The
last one is deliberately named distinctly from `record_downloaded_file`
(which never overwrites, per the dedup rule below) — it exists solely for
these two repair modes to correct a row after finishing a post-processing
step that didn't complete the first time.

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
- **Audiobook episode extraction** (`downloader/audiobook_processor.py::extract_episode_info`):
  tries three patterns in order. (1) "Ep <n> - <subtitle>" (with a
  specific "trailing uploader tag" peel-off rule — see the two regex
  comments, the space-before-hyphen distinction is load-bearing, don't
  simplify it away). (2) "Vol <n> <subtitle>" (or "Volume"/"vol.") — a
  whole compiled book bundling many chapters into one file. This gets
  `EpisodeInfo(label="Vol", pad_width=2)` instead of the default
  `("Ep", 4)` — a *different label and number space*, so a volume can
  never collide with, or render indistinguishably from, a same-numbered
  chapter (Volume 1 and chapter Ep 1 are unrelated; both can coexist).
  `infer_next_episode_number`/`parse_tagged_episode_number` deliberately
  only recognize "Ep", not "Vol", so volume numbers never leak into
  chapter-number inference or vice versa. (3) A cleanly-delimited bare
  number/range *anywhere* in the filename stem — leading, trailing, or
  the whole stem (e.g. "1114", "5-6", "Shadow Slave 1751-1846" — trailing
  range with a title prefix — or "0001_0100_Weakest_Beast_Tamer" —
  leading range with a title suffix, "_" as the separator — using the
  range's start). "Cleanly-delimited" means bounded by the stem's edges
  or a whitespace/underscore/hyphen/dot separator on each side — a digit
  run merely adjacent to other text without such a boundary doesn't
  count. Returns `None`, not a guess, when nothing matches —
  `process_audiobook_file` only then falls back to
  `infer_next_episode_number` (highest existing "Ep n" in the destination
  dir + 1). The Telegram message ID is *never* used as an episode number —
  it's an arbitrary ID shared across the whole chat, unrelated to the
  show's own numbering; an earlier version of this code did use it as the
  fallback, which is why `--mode verify` (§2) exists at all.
- **`apply_episode_tagging` vs. `process_audiobook_file`**: the former
  tags+moves using an *explicitly-given* `EpisodeInfo`, never re-deriving
  it from `file_path`'s current name. This split matters — a file being
  corrected already has the wrong "Ep <n>" baked into its current
  filename, which `extract_episode_info` would happily re-match if given
  the chance. `process_audiobook_file` (the normal per-download path)
  derives `info` from the source's raw filename and calls
  `apply_episode_tagging`; `episode_verifier` derives `info` from
  Telegram's message instead and calls the same primitive.
- `shutil.move`, not `os.rename`, specifically to survive cross-filesystem
  relocation (`EXDEV`) in both of the above.

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
kept the whole suite at ~2-3 seconds for 156 tests with zero flakiness from
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

## 10. Lessons learned this session — recommended C# design improvements

The audiobook episode-numbering logic didn't arrive at its current shape
by design — it got there through several rounds of real production bugs
found by actually running the tool against a real library. That history
is worth preserving as *design input* for the C# port, so the rewrite
starts from the end state instead of re-discovering the same lessons the
hard way. None of this has been refactored into the Python codebase
(scope was kept to fixing the live bugs, not restructuring working code)
— treat this section as "build it this way from scratch," not "the
Python code already looks like this."

**What went wrong, in order, and why it matters:**
1. Episode numbering originally fell back to the Telegram message ID when
   a filename had no "Ep n" pattern. Message IDs are an arbitrary,
   chat-wide counter — completely unrelated to a show's chapter count.
   This silently mistagged ~30 real files before it was caught, because
   nothing distinguished "confidently parsed" from "desperately guessed."
2. The fix (parse a bare number from the filename) was first written to
   only look at the *trailing* position. Real files had the number
   *leading* instead (`0001_0100_Title.mp3`) — missed entirely, silently
   fell through to an even-worse fallback ("guess the next sequential
   number"), producing plausible-looking but wrong numbers.
3. A further fix generalized the number search to scan the whole
   filename — but then a *volume* file (`Title Volume 10 Subtitle.m4a`,
   a whole book bundling many chapters) got matched by the generic
   number-in-filename rule and tagged as chapter `Ep 10`, silently
   colliding in meaning (not just filename) with the real chapter 10.
4. Recovering from step 1 and step 3's mistakes required manually
   reconstructing the correct numbers from external evidence (the
   original filenames, still visible in a terminal scrollback) — there
   was no built-in way to preview what a bulk re-tag operation *would*
   do before it did it.

**Concrete recommendations for the C# domain model:**

- **Separate "source identity" from "domain numbering" as distinct
  types, structurally.** e.g. a `MessageReference` (chat ID + message ID,
  provenance/logging only) and a `ChapterNumber`/`VolumeNumber` value
  type that can *only* be constructed from a successful filename parse or
  an explicit inference step — never from a `MessageReference`. If the
  compiler won't let you pass a message ID where a chapter number is
  expected, bug #1 becomes structurally impossible instead of a runtime
  footgun.
- **Model content-unit kind as a real type**, e.g.
  `enum ContentUnitKind { Chapter, Volume }` on day one, not a string
  label bolted on after the fact (`EpisodeInfo.label` in the Python code
  is exactly that retrofit — functional, but a smell). Each kind should
  own its own number space, padding width, and destination-naming rule as
  data on the enum case (or a small strategy object per kind), so adding
  a third kind later (e.g. "Side Story") doesn't require touching
  unrelated code paths.
- **Filename parsing as an ordered chain of small, independently
  testable strategies**, not one growing function with sequential
  if/elif fallbacks (`extract_episode_info` in Python is currently this;
  it works, but every new real-world filename shape meant editing the
  same function and re-reasoning about interaction with every earlier
  branch). In C#: an ordered list of `IFilenameParser` implementations
  (`ChapterPatternParser`, `VolumePatternParser`, `BareNumberParser`,
  ...), each takes a filename and returns a `ParseResult?`. Adding a new
  shape means adding a new class to the list, not editing an existing
  one. Order matters (most specific first) — make that order an explicit,
  documented, tested property of the list itself.
- **Make every parse result carry *why*, not just *what*.** A
  `ParseResult` with `{ Number, Subtitle, Kind, MatchedBy: string,
  Confidence: enum }` (or similar) turns "why did this file get tagged
  chapter 10 instead of volume 10" from an archaeology exercise (as it
  was in this session) into a log line. The Python version has no
  equivalent — `extract_episode_info` returns `None` or a bare
  `EpisodeInfo`, with no record of which branch fired.
- **Ship the real-world filename corpus this session discovered as the
  parser's test suite from day one**, not something to rediscover file by
  file against a live library:
  - `Ep 2027 - The Strength of the Wolf.mp3` (labeled chapter, subtitle)
  - `1114.m4a`, `1114..m4a` (bare number, incl. upstream double-dot artifact)
  - `5-6.m4a` (bare range, hyphen separator)
  - `Shadow Slave 1751-1846.m4a` (title prefix + trailing range)
  - `0001_0100_Weakest_Beast_Tamer.mp3` (leading range, underscore separator,
    title suffix)
  - `Shadow Slave Volume 10 Dark Lord's Dreadful Travelogue.m4a` (volume,
    not a chapter — must not collide with chapter 10)
  - `random_upload_name.mp3`, `totally_untitled_file.mp3` (unparseable —
    must return "no match," never a guess)
- **Build state-repair as a first-class subsystem from the start**, not
  bolted on after data is already wrong. The Python `--mode
  reprocess`/`--mode verify` pair (§2, §4) only exist *because* bugs
  #1–#3 happened to real data — design the C# port assuming that kind of
  reconciliation tool will always eventually be needed for a system that
  derives structured metadata from unstructured, uploader-controlled
  filenames, and build the "find files whose current state disagrees
  with what re-deriving from source would produce" capability alongside
  the primary pipeline, not after the first incident.
- **Add a dry-run/preview mode to any bulk re-tag or state-repair
  operation before it touches files.** Step 4 above — manually
  reconstructing lost data from a terminal scrollback — would have been
  unnecessary with a `--dry-run` flag that prints "file X would become Y
  (source: bare-number parser, confidence: high)" for review before
  committing. This is cheap to build in from the start and expensive to
  wish for after a bad run.
- **Keep the good parts.** Several things about the Python version held
  up well under real usage and are worth preserving as-is, not just
  improving on: atomic `.tmp` writes, the FloodWait retry shape, the
  never-overwrite dedup-suffix rule (this is *why* the mistagging
  incidents above never lost data, only mislabeled it — every wrong
  guess just landed as its own separate file rather than clobbering a
  correct one), and testing with hand-written fakes instead of a mocking
  framework.

## 11. Things to explicitly re-verify before porting (don't trust this doc)

This file is a snapshot, not a live contract. Before treating any claim
above as ground truth for the port:
- Re-run `git log --oneline` and diff against this file's "last updated"
  commit to see what's changed since.
- Re-run `pytest -q` and `mypy --strict src` to confirm the "clean" claims
  in §8 still hold.
