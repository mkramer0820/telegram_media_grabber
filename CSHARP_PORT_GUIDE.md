# C# Port Guide

**What this is**: a standalone instruction set for starting the C# rewrite of
this project — architecture, service boundaries, persistence, DI, testing,
and the one new feature (metadata overrides) that came up in design
discussion but was deliberately *not* built into the Python version. Written
so you can take this document alone into a new repo or a new branch and have
everything needed to start writing code, without re-reading the whole
Python history.

Companion document: `PROJECT_STATE.md` describes what the *existing Python
app* does today, byte for byte — read that first if you need to know "what
does the current tool actually do," then use *this* document for "how should
the C# version be built." This document assumes you've read `PROJECT_STATE.md`
§5 (algorithms to preserve) and §10 (lessons learned) at least once.

Written: 2026-08-02. Nothing has been implemented against this guide yet —
it is a plan, not a status report.

---

## 1. Scope and goals

- Same functionality as the Python app: download media from Telegram
  channels, upload local files to Telegram, audiobook tagging/organization,
  state tracking, a terminal UI.
- Primary motivation: a **lighter-weight distributable** — a single
  self-contained executable, no interpreter/runtime install required for
  end users, smaller Docker image if still containerized.
- Secondary motivation: fix the architectural rough edges that only became
  visible after real usage (see `PROJECT_STATE.md` §10) — this is not a
  literal transliteration, it's a redesign informed by hindsight.
- **Config-compatible**: read the same `channels.yaml`/`.env` shape (see §7
  below and `PROJECT_STATE.md` §7) so existing users migrate with zero
  config changes. This is a hard constraint, not a nice-to-have.
- **Telegram session is NOT portable.** Whatever MTProto library is chosen
  (WTelegramClient recommended — see `PROJECT_STATE.md` §6) will need its
  own first-run login. Communicate this to users; don't try to convert
  Telethon's `.session` SQLite format.

---

## 2. New feature: metadata overrides (design only — not yet built anywhere)

This surfaced from a real need: the automatic filename-parsing pipeline
(`PROJECT_STATE.md` §10) will never get every case right, because this tool
runs on other people's personal media libraries with their own naming
conventions and preferences. Rather than continuing to special-case the
parser for every new filename shape (the pattern that caused the whole
bug chain in §10), give the user an explicit, config-driven escape hatch.

### Shape

Add an `overrides` section, either per-channel (in the channel's block) or
as a separate top-level file (`config/overrides.yaml`) if it grows large.
Recommend **per-channel, in `channels.yaml`**, to start — same file the
user already edits for everything else, same fail-fast validation model.

```yaml
channels:
  - id: "shadowslavexfm77"
    name: Shadow.Slave
    audiobook_mode: true
    metadata:
      author: "Guiltythree"
      novel_title: "Shadow Slave"
    overrides:
      # Keyed by the exact ORIGINAL filename as it arrives from Telegram
      # (before sanitization) — the same string extract_episode_info sees.
      - match: "weird_upload_name_247.mp3"
        kind: chapter            # "chapter" | "volume"
        number: 247
        subtitle: "The Real Title Someone Forgot To Put In The Filename"
      - match: "Shadow Slave Compendium.m4a"
        kind: volume
        number: 12
        subtitle: "Compendium"
      - match: "duplicate_upload.mp3"
        skip: true                # never process this file at all
```

### Precedence (must be explicit and documented, not implicit)

1. **Override match** (exact filename match) — wins unconditionally if
   present. `skip: true` short-circuits everything else for that file.
2. **Parsed** — the ordered `IFilenameParser` chain from
   `PROJECT_STATE.md` §10.
3. **Inferred** — highest existing number + 1, chapter-kind only.

### Validation rules (mirror the existing "fail loudly on typo" philosophy)

- Unknown keys in an override entry → config load fails at startup, not a
  silent no-op. (Same principle as `ConfigDict(extra="forbid")` in the
  Python `Settings`/`ChannelConfig` models — see `PROJECT_STATE.md` §7.)
- `kind: volume` without there being any sensible padding default is fine
  (padding is derived from `kind`, not user-specified) — don't expose
  `pad_width` as a user-facing knob, it's an implementation detail of the
  `Chapter`/`Volume` enum (§4 below).
- `match` must be unique within a channel's override list — duplicate
  `match` entries should fail config validation, not silently pick one.
- An override `match` that never actually matches any real file it
  encounters is *not* an error (channels get new content; overrides may
  be written pre-emptively or become stale) — but consider a `--mode
  verify`-style report that lists unused overrides, so stale entries are
  discoverable rather than invisible cruft.

### Where it plugs into the pipeline

The override lookup should sit as its own `IFilenameParser`-shaped
component, but tried **first**, before the parser chain — same interface
(`ParseResult?`), so the rest of the pipeline (tagging, path-building,
state recording) doesn't need to know or care whether a result came from
an override or a real parse. Keep `ParseResult.MatchedBy` distinct
(`"override"` vs. a parser class name) so the "why did this get tagged
this way" traceability goal from §10 still covers manually-overridden
files, not just parsed ones.

### Explicitly out of scope for v1

- Folder-level or pattern/wildcard-based override rules (e.g. "everything
  in this directory gets author X"). Start with exact-filename matching
  only — simpler to reason about, simpler to validate, covers the actual
  need (a handful of stubborn files per library, not systematic
  relabeling). Revisit only if real usage shows exact-match isn't enough.
- Overriding non-audiobook metadata (plain download-mode channels don't
  have a metadata pipeline to override).
- A UI for editing overrides. Config-file-only, like everything else in
  this app.

---

## 3. Recommended project structure (Clean-Architecture-flavored, not dogmatic)

```
TelegramMediaGrabber/
├── TelegramMediaGrabber.sln
├── src/
│   ├── TelegramMediaGrabber.Domain/           # POCOs, enums, value objects. Zero dependencies.
│   │   ├── ContentUnitKind.cs                 # enum { Chapter, Volume } — see §4
│   │   ├── ChapterNumber.cs                   # value type, only constructible via parse/infer
│   │   ├── ParseResult.cs                     # { Number, Subtitle, Kind, MatchedBy, Confidence }
│   │   ├── MessageReference.cs                # chat id + message id — provenance only
│   │   └── ...
│   ├── TelegramMediaGrabber.Application/      # Interfaces + orchestration services. Depends on Domain only.
│   │   ├── Parsing/
│   │   │   ├── IFilenameParser.cs
│   │   │   ├── ChapterPatternParser.cs
│   │   │   ├── VolumePatternParser.cs
│   │   │   ├── BareNumberParser.cs
│   │   │   ├── OverrideParser.cs              # tried first — see §2
│   │   │   └── FilenameParserChain.cs         # ordered composite, itself an IFilenameParser
│   │   ├── Audiobook/
│   │   │   ├── IAudiobookTagger.cs
│   │   │   ├── AudiobookProcessingService.cs  # orchestrates parse -> tag -> move -> record
│   │   │   ├── IReprocessService.cs           # --mode reprocess equivalent
│   │   │   └── IVerifyService.cs              # --mode verify equivalent
│   │   ├── Downloading/
│   │   │   └── IDownloadManager.cs
│   │   ├── Uploading/
│   │   │   └── IUploadManager.cs
│   │   └── State/
│   │       └── IStateRepository.cs
│   ├── TelegramMediaGrabber.Infrastructure/   # Concrete implementations. Depends on Application + Domain.
│   │   ├── Telegram/
│   │   │   ├── WTelegramClientWrapper.cs      # implements a thin ITelegramClient
│   │   │   └── FloodWaitRetryPolicy.cs
│   │   ├── Persistence/
│   │   │   ├── SqliteStateRepository.cs
│   │   │   └── Migrations/                    # see §5
│   │   ├── Tagging/
│   │   │   └── TagLibAudiobookTagger.cs       # wraps TagLibSharp
│   │   └── Configuration/
│   │       ├── ChannelsYamlLoader.cs          # YamlDotNet, mirrors ChannelsFile/ChannelConfig
│   │       └── OverridesValidator.cs
│   ├── TelegramMediaGrabber.Cli/              # Composition root + Spectre.Console UI. Depends on everything.
│   │   ├── Program.cs                         # Generic Host setup, DI registration, mode dispatch
│   │   ├── Commands/                          # download / upload / reprocess / verify
│   │   └── Ui/
│   │       ├── DownloadDashboard.cs
│   │       └── UploadDashboard.cs
│   └── ...
└── tests/
    ├── TelegramMediaGrabber.Domain.Tests/
    ├── TelegramMediaGrabber.Application.Tests/    # parser chain, tagging orchestration — hand-written fakes
    └── TelegramMediaGrabber.Infrastructure.Tests/ # SQLite repo tests against temp-file DBs
```

**Dependency direction** (same rule as the Python `CLAUDE.md`, just spelled
in C# project-reference terms): `Cli` → `Infrastructure` → `Application` →
`Domain`. `Domain` has zero project references. Nothing in `Application`
references `Infrastructure` or `Cli` — it depends only on interfaces it
defines itself, implemented elsewhere. This is what makes the parser chain,
tagging service, etc. testable with fakes instead of a real Telegram
connection or real SQLite file (though real SQLite-against-tmp-file is
still preferred for the repository tests themselves — see §8).

Four projects is a reasonable size for this app's scope — don't split
further pre-emptively (e.g. don't make `Domain.Parsing` its own assembly).
If a project's own line count starts creeping toward the kind of size that
made `audiobook_processor.py` a smell (`PROJECT_STATE.md` §10), that's the
signal to split a *namespace* into its own file/class, not necessarily a
new project.

---

## 4. Domain model specifics

```csharp
public enum ContentUnitKind { Chapter, Volume }

public readonly record struct ChapterNumber
{
    public int Value { get; }
    public ContentUnitKind Kind { get; }

    // No public constructor taking a bare int from arbitrary call sites.
    // Only ever created by ParseResult.ToChapterNumber() or an explicit
    // InferNext() call — never from a MessageReference. This is the
    // structural fix for bug #1 in PROJECT_STATE.md §10: there is no
    // code path by which a Telegram message ID can become a ChapterNumber.
    internal ChapterNumber(int value, ContentUnitKind kind) { ... }

    public string Label => Kind switch
    {
        ContentUnitKind.Chapter => "Ep",
        ContentUnitKind.Volume => "Vol",
        _ => throw new UnreachableException(),
    };

    public int PadWidth => Kind switch
    {
        ContentUnitKind.Chapter => 4,
        ContentUnitKind.Volume => 2,
        _ => throw new UnreachableException(),
    };
}

public readonly record struct MessageReference(long ChatId, int MessageId);

public sealed record ParseResult(
    ChapterNumber Number,
    string? Subtitle,
    string MatchedBy,       // parser class name, or "override"
    ParseConfidence Confidence
);

public enum ParseConfidence { Exact, Inferred }
```

Keep `Label`/`PadWidth` as computed properties on the enum-driven type, not
separate fields threaded through every call site (this is a deliberate
improvement over the Python `EpisodeInfo.label`/`pad_width` fields, which
work but are a retrofit — see `PROJECT_STATE.md` §10).

---

## 5. Persistence and sessions

- **SQLite access**: use **Dapper** (or raw `Microsoft.Data.Sqlite` ADO.NET)
  over EF Core. Three tables, no relational complexity, no need for EF's
  change-tracking or LINQ-to-SQL translation overhead — EF Core would be
  solving a problem this app doesn't have, at the cost of startup time and
  a learning-curve/debugging surface. This mirrors the Python choice
  (stdlib `sqlite3`, not an ORM).
- **Single-writer discipline**: the Python version serializes all writes
  through one `asyncio.Lock` around one connection. In C#, prefer a
  **`System.Threading.Channels.Channel<T>`-based writer queue** (a single
  background task drains the channel and performs writes) over a raw
  `SemaphoreSlim` — it's the more idiomatic async .NET pattern for "many
  producers, one serialized consumer," and it decouples "request a write"
  from "the write actually happening," which makes shutdown/draining
  cleaner (await the channel completing instead of hoping a semaphore
  isn't held). A `SemaphoreSlim(1,1)` around the connection is an
  acceptable, simpler fallback if the channel approach feels like
  overkill when this is actually built — either is fine, just pick one
  and be consistent; don't do both.
- **WAL mode**, same as Python (`PRAGMA journal_mode=WAL`).
- **Schema migrations**: even at 3 tables, don't hand-edit `CREATE TABLE
  IF NOT EXISTS` forever the way the Python version does (it works, but
  every schema change becomes an ad-hoc `ALTER TABLE` reasoned about by
  hand). Use a lightweight embedded-SQL-scripts-plus-`schema_version`-table
  approach (roll your own — this doesn't need a full migration framework
  like a heavier tool such as FluentMigrator/DbUp unless the schema is
  expected to grow substantially; DbUp is a reasonable lightweight choice
  if you want one).
- **Telegram session**: WTelegramClient's own session file format (not
  compatible with Telethon's). Store it under the same `data/` directory
  convention the Python version uses (`PROJECT_STATE.md` §3's note on
  `data/` being "everything Telegram/local-state related"). Never commit
  it; same `.gitignore` posture as today.
- **The `downloaded_files.file_path`-correction pattern**
  (`update_downloaded_file_path`, `find_downloaded_record_by_path`,
  `list_downloaded_records` — `PROJECT_STATE.md` §4) should exist in the
  C# repository interface from day one, not bolted on later — this is
  what `IReprocessService`/`IVerifyService` are built on, and per §10,
  those aren't optional extras, they're load-bearing for a tool that
  derives metadata from uploader-controlled filenames.

---

## 6. Dependency injection, configuration, and hosting

- **Generic Host** (`Microsoft.Extensions.Hosting`) as the composition
  root in `Program.cs`, even for a CLI tool — gives you `IHostedService`
  lifecycle, built-in `IConfiguration`/`IOptions` binding, built-in
  `Microsoft.Extensions.Logging`, and graceful shutdown via
  `IHostApplicationLifetime` (maps to the Python `KeyboardInterrupt`
  handling in `main.py`) for free instead of hand-rolling all of it.
- **Configuration binding**: `IConfiguration` + a YAML configuration
  provider (there are a couple of community `Microsoft.Extensions.
  Configuration.Yaml`-style packages; if none feel trustworthy enough,
  parsing via `YamlDotNet` directly into POCOs and validating manually is
  a fine fallback — don't fight the ecosystem here). Bind into strongly
  typed options classes (`ChannelsOptions`, `ChannelOptions`,
  `UploadJobOptions`) and **validate with `IValidateOptions<T>`** at
  startup — this is the direct equivalent of pydantic's `extra="forbid"`
  fail-fast behavior (`PROJECT_STATE.md` §7) and must be preserved: an
  unknown YAML key or a typo'd field name should crash the app at startup
  with a clear message, never silently no-op.
- **Register everything by interface**, constructor injection throughout:
  `IFilenameParser` (as a chain — register the concrete parsers, then a
  factory/composite that orders them, or use `IEnumerable<IFilenameParser>`
  injection with an explicit `Order` property per implementation if you
  want registration order to not matter), `ITelegramClient`,
  `IStateRepository`, `IAudiobookTagger`, `IProgressReporter`, etc.
  Avoid a service locator / `IServiceProvider.GetService` scattered
  through business logic — inject what a class needs, nothing reaches
  into the container itself outside `Program.cs`.
- **`IProgressReporter`-style abstraction** over the UI (mirrors the
  Python `Protocol`-based `ProgressReporter`/`UploadProgressReporter` —
  `PROJECT_STATE.md` §3's dependency-direction note): the download/upload/
  reprocess/verify services must not reference `Spectre.Console` types
  directly. Define the callback interface in `Application`, implement the
  actual dashboard in `Cli`.
- **Mode dispatch** (`download`/`upload`/`reprocess`/`verify`): a
  `System.CommandLine`-based CLI (or the simpler manual `args[0]` switch,
  matching Python's `argparse` choices) that resolves the right
  `IHostedService`/command handler via DI rather than a big if/else in
  `Main`.

---

## 7. Configuration surface (must match the Python schema)

Reproduce exactly (see `PROJECT_STATE.md` §7 for full field list):

```yaml
download_root: downloads
max_concurrent_downloads: 5
channels: [ ChannelConfig, ... ]     # + optional `overrides` per §2 above
upload_jobs: [ UploadJobConfig, ... ]
```

`.env`/environment variables: `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`,
`TG_SESSION_NAME`, `AUDIOBOOKS_DEST_DIR` — same names, so a user's existing
`.env` works unmodified. Bind these via `IConfiguration`'s environment
variable provider + a `.env` file loader (e.g. `DotNetEnv` package) rather
than inventing a new secrets mechanism.

---

## 8. Testing strategy

- **xUnit**, hand-written fakes for `ITelegramClient` and Telegram message
  objects — mirror the Python approach exactly
  (`PROJECT_STATE.md` §8: fakes over mocking frameworks, because the
  Python version found this kept tests fast and free of mock-mismatch
  flakiness). Reach for a mocking library (NSubstitute/Moq) only at true
  external boundaries if a fake would be nontrivial to hand-write — not
  as the default.
- **Repository tests** (`IStateRepository` implementation) run against a
  real temp-file SQLite database per test, not an in-memory fake — same
  reasoning as the Python `tmp_path` fixture pattern: you want to catch
  real SQL/schema bugs, not just interface-shape bugs.
- **Ship `PROJECT_STATE.md` §10's filename corpus as the parser chain's
  test suite from the first commit** — don't wait to rediscover these
  cases against a live library a second time in a different language.
- **`dotnet test` + nullable reference types + analyzers as the strict
  gate**, equivalent to `mypy --strict`: enable `<Nullable>enable</Nullable>`
  project-wide, treat warnings as errors in CI
  (`<TreatWarningsAsErrors>true</TreatWarningsAsErrors>`), and consider
  `Microsoft.CodeAnalysis.NetAnalyzers` at a strict ruleset. No `dynamic`,
  no unchecked nullable suppression (`!`) without a comment explaining why
  — same spirit as the Python project's two documented
  `# type: ignore[no-untyped-call]` exceptions.

---

## 9. Dry-run support (build this in from the start — see PROJECT_STATE.md §10)

Every mutating service that touches files or rewrites state
(`IReprocessService`, `IVerifyService`, the override-driven re-tag path,
and arguably `IAudiobookTagger` itself) should accept a `dryRun: bool`
(or a small `IExecutionContext { bool DryRun }` threaded through DI as a
scoped service) and, when true, report exactly what *would* happen —
`ParseResult` including `MatchedBy`/`Confidence` — without touching disk
or the database. This is cheap to design in from day one and was the one
capability most missed during this session's real incident (`PROJECT_STATE.md`
§10, point 4): recovering mistagged files required manually reconstructing
lost information from a terminal scrollback, which a `--dry-run` preview
would have made unnecessary.

---

## 10. Distribution

- `dotnet publish -p:PublishSingleFile=true --self-contained -r <RID>` for
  the "lightweight distributable" goal — a single executable per platform,
  no .NET runtime install required on the user's machine. This is the
  concrete payoff of the port; make sure the final packaging step actually
  produces this, not just a `dotnet run`-only artifact.
- If still shipping Docker as an option (`PROJECT_STATE.md` mentions the
  Python version's `Dockerfile`/`docker-compose.yml`/`.dockerignore`),
  base the image on a minimal runtime-deps image or the self-contained
  single-file binary directly — the whole point of the port is a smaller
  footprint than the `pip install` layer, don't undo that by pulling in
  the full SDK image at runtime.

---

## 11. Repo/branch setup

Recommend a **new repository** over a long-lived branch in this one:
cleaner git history (no interleaved Python/C# commits), independent
`.gitignore`/CI needs (no reason for a C# repo to carry Python's
`requirements.txt`/`.venv` patterns or vice versa), and it makes clear to
anyone looking at either repo which one is "the current thing." If a
branch is preferred instead for continuity reasons, use a long-lived
`csharp-port` branch off `main` so the Python `main` branch stays
independently deployable/maintainable during the transition — don't work
directly on `main` for the port.

Either way: copy `PROJECT_STATE.md` and this file (`CSHARP_PORT_GUIDE.md`)
into the new repo/branch as the starting reference documents, and copy
`config/channels.example.yaml` as the schema-compatibility source of
truth (§7).

---

## 12. Things this guide does NOT cover (deliberately)

- A literal line-by-line translation plan — `PROJECT_STATE.md` §9
  ("Suggested porting order") already covers build sequencing; this guide
  is about *shape*, not *order*. Read both.
- UI visual design — `Spectre.Console` is the recommended library
  (`PROJECT_STATE.md` §6); how the dashboards actually look is an
  implementation detail to work out when building `Cli`, not a
  pre-planning concern.
- Telemetry/analytics — not present in the Python version, no evidence
  it's needed; don't add it speculatively.
