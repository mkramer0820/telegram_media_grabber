# CLAUDE.md

This file defines **unbreakable rules** for any human or AI agent (Claude Code included)
working on this codebase. It is not a style guide — it is a contract. Violating any
"MUST" rule below is a bug, even if the code runs.

This project is the **Telegram Batch Media Downloader**. See `README.md` for
architecture, features, and directory layout.

---

## 1. Code Standards

1. **Strict typing is mandatory.** Every function, method, and module-level variable
   must carry full type hints. The project MUST pass `mypy --strict` with zero errors.
   `Any` is forbidden unless annotated inline with `# type: ignore[...]` and a comment
   explaining why no better type exists.
2. **Docstrings are mandatory** on every public module, class, and function
   (Google-style). A docstring states *why*/*contract* (params, returns, raises), not a
   restatement of the function name. Private helpers (`_foo`) get a one-line docstring
   minimum.
3. **Modular design.** One responsibility per module. Business logic (downloading,
   state tracking, config) MUST NOT import from `src/ui/`. UI code depends on core
   modules, never the reverse. No module may exceed ~400 lines — split it if it does.
4. **No bare `except:`.** Catch specific exception types. If you must catch broadly at
   a boundary (e.g. the main worker loop), catch `Exception`, log it with context, and
   re-raise or handle explicitly — never swallow silently.
5. **No global mutable state.** Configuration and runtime state are passed explicitly
   (constructor injection / function args), not read from module-level singletons,
   except for the single `Settings` object assembled once in `main.py`.
6. **Dependencies are explicit.** Every import used must appear in `requirements.txt`
   pinned to a minimum compatible version (`>=x.y.z`). No silent reliance on transitive
   dependencies.
7. **All new code must have corresponding tests** under `tests/`, mirroring the
   `src/` package structure. No PR/commit is "done" without tests for the new
   behavior.

## 2. File Management Rules

1. **Atomic writes only.** Any file written to disk (media file, SQLite DB, config
   cache, session backup) MUST be written to a temporary path in the same directory
   (suffix `.tmp` or `.part`) and then moved into place with `os.replace()` /
   `Path.replace()`, which is atomic on both POSIX and Windows. Never write directly to
   the final filename.
2. **No partial files survive a crash.** If a download is interrupted, the `.part`
   file MUST remain (for resume) or be deleted — it must never be renamed to the final
   name unless byte-complete and verified (size and, where available, hash match).
3. **Filename sanitization is mandatory and centralized.** All filenames derived from
   Telegram metadata (captions, original filenames, usernames) MUST pass through a
   single sanitizer function (`src/downloader/filenames.py` or equivalent) before
   touching the filesystem. That function MUST:
   - Strip/replace characters illegal on Windows (`< > : " / \ | ? *`, control chars)
     even when running on Linux/macOS, since output must remain portable.
   - Forbid reserved Windows device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`,
     `LPT1-9`) by suffixing them.
   - Truncate to a safe max length (255 bytes) while preserving the file extension.
   - Never allow path traversal (`..`, absolute paths, leading `/` or drive letters)
     from remote-controlled input.
4. **Deduplication must never lose data.** Before writing, compute a stable dedup key
   (Telegram `(chat_id, message_id)` at minimum; content hash where feasible for
   cross-message duplicate detection). If a filename collision occurs between two
   *distinct* dedup keys, the new file MUST be suffixed (`name (1).ext`, `name (2).ext`,
   ...) — never overwritten. Overwriting a file without first confirming it is the same
   logical media is forbidden.
5. **State and files must stay consistent.** A media file is only considered
   "downloaded" in SQLite state after the atomic rename succeeds. Never mark state as
   complete before the file is durably on disk.

## 3. Logging vs. UI Constraints

1. **`print()` is banned everywhere in `src/`.** No exceptions. All user-facing
   terminal output MUST go through `rich` (`rich.console.Console`, `rich.progress`,
   `rich.table`, etc.), routed through the `src/ui/` layer.
2. **Backend logs never touch the terminal directly.** Application logging (via the
   standard `logging` module) MUST be configured to write only to a rotating file
   handler (`logging.handlers.RotatingFileHandler`, e.g. `logs/app.log`, capped size +
   backup count). Logging MUST NOT attach a `StreamHandler` to stdout/stderr — that
   would corrupt `rich` progress bars and live displays.
3. **If a log message needs to be user-visible**, it is surfaced deliberately through
   the `rich` UI layer (e.g. a status line, a table row, a toast-style message) — it is
   a UI concern, not a side effect of the logging call.
4. **Use module-level loggers** (`logging.getLogger(__name__)`), never the root logger
   directly, so log origin is always traceable.
5. **Exceptions surfaced to the user** go through the UI layer's error rendering (e.g.
   `console.print` with `rich` styling or a dedicated error panel), while the full
   traceback is always written to the log file regardless of what the UI shows.

## 4. Concurrency Rules

1. **All I/O-bound Telegram/network work is `asyncio`-based**, built on Telethon's
   async client. Blocking calls (disk I/O for large files, SQLite writes) MUST be
   offloaded via `asyncio.to_thread` / `loop.run_in_executor` when they risk blocking
   the event loop for a non-trivial duration.
2. **Exactly one event loop.** The loop is created and owned by `src/main.py`
   (`asyncio.run(...)`). No module below `main.py` may call `asyncio.run()` or
   otherwise create a competing loop.
3. **State saving (SQLite) must be thread-/task-safe.** Concurrent downloader workers
   MUST NOT write to the SQLite connection concurrently from multiple threads. Use
   either a single writer task consuming a queue, or a per-thread connection with
   `PRAGMA journal_mode=WAL` and short-lived transactions — never share one
   `sqlite3.Connection` object across threads without serialization.
4. **Every concurrent worker must be bounded.** Use `asyncio.Semaphore` to cap
   concurrent downloads; unbounded `asyncio.gather` over an arbitrary number of
   messages is forbidden.
5. **Cancellation must be clean.** Workers MUST handle `asyncio.CancelledError` (e.g.
   on `Ctrl+C`) by finishing or safely aborting the current atomic write (per Section 2)
   before the process exits — never leave a half-renamed file or a half-committed DB
   transaction.
6. **`FloodWaitError` and other rate-limit errors are handled at the worker level**,
   not swallowed globally: back off for the exact duration Telegram specifies, surface
   the wait via the UI layer, and resume — never retry in a tight loop.

---

## Summary Checklist (for quick review)

- [ ] `mypy --strict` passes, all public symbols typed and documented
- [ ] No `print()` anywhere in `src/`
- [ ] Logging goes to rotating file only, never stdout/stderr
- [ ] All disk writes are atomic (`.tmp`/`.part` + `os.replace`)
- [ ] Filenames sanitized through one shared function
- [ ] No overwrite on filename collision between distinct items
- [ ] Single event loop, owned by `main.py`
- [ ] SQLite writes are serialized/thread-safe
- [ ] Concurrent downloads bounded by a semaphore
- [ ] `FloodWaitError` handled gracefully with proper backoff
