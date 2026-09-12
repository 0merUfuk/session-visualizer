# Operational guide

This guide describes the `rifja` console interface for release **0.3.0**. Install with `brew install 0merUfuk/rifja/rifja` using the [README instructions](../README.md). Homebrew manages Python 3.14+, Git and the private environment; no manual Python setup is needed. Runtime commands use local files and Git; they do not contact a model or network service, execute transcript instructions, or run tests for your project.

## Initialize and choose state

```sh
rifja init
rifja setup --timezone Europe/Istanbul
rifja config
rifja config timezone UTC
rifja doctor
```

`rifja init` is the guided first-run path for an interactive terminal. It proposes each step and asks before every state change: writing the configuration with a detected or explicitly chosen timezone, registering each discovered session source, registering project directories you name, and running the first refresh as a separate explicit consent step. Registering a source reads nothing; only `refresh` imports. When Claude Code's source is offered, the wizard discloses its transcript deletion window (30 days by default) in one line and states that a durable copy exists only after `rifja refresh` imports the registered source — run refresh before that window expires. Without an interactive terminal (or with `--json`) `init` prints this plan, changes nothing and exits 2; `setup` stays the non-interactive primitive. Re-running `init` is safe: already-registered sources are skipped and refresh is incremental. Commands run against uninitialized state say so and point at `rifja init` in human output.

Use an IANA timezone such as `UTC`, `Europe/Istanbul` or `America/New_York`. `daily` groups timestamps into calendar days in this configured zone, including daylight-saving transitions. Original timestamps remain in evidence. A timestamp without an offset stays unresolved; the collector does not assign the host machine's timezone to it.

Without `--timezone`, setup detects the zone instead of assuming UTC. The chain is: the `TZ` environment variable first, then the `/etc/localtime` symlink target; each value is validated as an IANA key, and a set-but-invalid `TZ` value ends the chain. If neither step resolves, setup applies UTC and human output marks it `(fallback — pass --timezone to set it explicitly)`. Human output always marks the origin as `(explicit)`, `(detected)` or `(fallback)`; JSON keeps the single `timezone` key holding the zone setup actually applied.

State location precedence is:

1. `--home PATH` on the command.
2. `RIFJA_HOME` in the environment.
3. On macOS, `~/Library/Application Support/Rifja`.
4. Elsewhere, `$XDG_DATA_HOME/rifja`, falling back to `~/.local/share/rifja`.

```sh
rifja --home "/path/to/separate state" setup --timezone UTC
rifja source list --home "/path/to/separate state" --json
```

Every invocation must select the intended state. `--home` overrides the environment for that command. A query can initialize or migrate application state when opening it; read-only collection refers to producer files and repository observation, not an immutable application database.

## Register projects and linked worktrees

```sh
rifja project add "/path/to/harbor" --name harbor
rifja project discover "/path/to/repositories" "/path/to/other repositories"
rifja project list --json
rifja project show harbor
rifja project show harbor --observe --json
```

Discovery examines only the supplied roots. Registering a repository also discovers its linked Git worktrees. `project show --observe` refreshes Git observations; without that flag it shows cached observations. Project names are convenient selectors; use the project ID if names are ambiguous.

Repository discovery is bounded: six directory levels, 10,000 visited directories, 50,000 entries and 1,000 repositories. The returned list does not establish exhaustive coverage of a large root. Register an omitted repository directly with `project add`.

Git common-directory and worktree identities establish working context. Similar repository names, remotes and paths mentioned in chat do not merge projects. Git observation does not establish personal authorship or correctness. For a moved checkout, register its current path and inspect the resulting worktree identity before correcting any unresolved associations.

`config roots '["/path/to/repositories"]'` edits the stored root list. It does not start a background scan. Use `project discover` with explicit roots to discover repositories.

## Discover and register session sources

```sh
rifja source discover
rifja source add codex "/path/to/codex/sessions"
rifja source add claude "/path/to/claude/projects"
rifja source add hermes "/path/to/hermes/state.db"
rifja source list --json
rifja refresh
```

`source discover` checks candidate locations and reports availability; it does not read or import their transcripts. Candidate roots respect `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and `HERMES_HOME`, with the usual hidden home directories as fallbacks. `source add` registers an existing file or directory. Only `refresh` imports configured sources.

Register the actual transcript subtree or database you intend to import. Codex archived sessions can be registered as a separate root. A Hermes profile database needs its own explicit path. Producer executables, a running agent and provider authentication are not required to read supported local stores.

Supported shapes and gaps are documented in [Provider format evidence and support boundaries](providers.md). This release reads Codex/Claude JSONL, Codex `.jsonl.zst` and the supported Hermes SQLite shape. Hermes JSON exports, unknown shapes, inaccessible sources, encrypted content and missing inherited history are not silently treated as complete imports. Availability and compatibility are separate questions.

Compressed Codex input uses Python 3.14's standard-library Zstandard decoder, without an additional runtime package. Each source has an 8 MiB decoder history-window limit, 256 MiB compressed/decompressed byte limits and a 30-second cooperative processing deadline. The deadline is checked between bounded operations; it is not an operating-system guarantee against a stalled filesystem call. Truncated, corrupt, dictionary-dependent or over-limit streams produce partial-coverage diagnostics. No plaintext decompression file is created. Changed compressed sources are replayed rather than resumed from a decompressed byte offset.

## Refresh and inspect coverage

```sh
rifja refresh --json
rifja source list --json
rifja refresh --verify
rifja refresh --rebuild
rifja doctor --json
```

Normal refresh uses checkpoints and skips unchanged sources. `--verify` rechecks source content even when recorded file metadata is unchanged. `--rebuild` replays configured sources while preserving durable user memory, corrections, provenance and forget rules. Neither option edits producer history or verifies project code.

Refresh prints progress to stderr as `refresh: …` lines (sources processed, records parsed and inserted, current path). On an interactive terminal one line updates in place at a bounded cadence; a redirected run appends plain lines at the same bounded cadence. The global `--quiet` flag suppresses progress output; the final report on stdout is unaffected, and in `--json` mode progress still goes to stderr while the envelope stays on stdout. The human summary names sources that finished `partial`, `failed` or `missing`; inspect them with `source list --json`.

An incomplete final JSONL record waits for a later refresh. Malformed records, missing sources and unsupported formats leave diagnostics and may yield partial coverage while valid captured records remain usable. Check `source list` for source paths, statuses and diagnostic codes. `doctor` checks application state integrity and source availability; a passing integrity check is not a claim that every source is complete.

A later valid append does not repair an earlier malformed or unsupported complete record. Its diagnostic remains partial until a replacement or rebuild actually rechecks the affected content. Completing a partial trailing line can clear that trailing-line diagnostic.

Coverage starts as `not_refreshed`. Changing configured sources or exclusions after a refresh reports `refresh_required` until the next refresh. A configured root that disappears is reported as unavailable, and otherwise unchanged refreshed coverage becomes `partial`. These inspections check configured path availability; they do not scan for new transcripts or import changes. `last_refresh` describes the earlier run and must be read alongside the current coverage status.

When a source is edited, replaced or truncated, previously captured content remains available with its source-generation provenance. An active, pending, blocked or proposed item found only in obsolete generations is marked `source_superseded`, retaining its former `historical_status`; it is excluded from current unfinished work and current decisions while remaining searchable. A missing source is different: known unresolved work from its current generation remains unfinished, with unavailable-evidence and coverage warnings. Neither condition proves completion. Use `explain RECORD_ID --json` to inspect the distinction. An explicit durable user correction can still override an imported item's state.

To add source exclusions:

```sh
rifja config exclusions '["*/scratch-exports/*", "*/do-not-import.jsonl"]'
```

Exclusions are path/basename patterns. Applying one can also forget already imported sessions associated with matching sources and create rules preventing their reimport. Durable user memory remains, but references to forgotten evidence become unavailable. Inspect the pattern and make a backup before applying an exclusion to existing state. This release has no separate `source remove` command.

## Inspect a day, task or project

```sh
rifja daily
rifja daily 2026-09-05 --project harbor
rifja daily 2026-09-01 --to 2026-09-05 --project harbor --json
rifja session --json
rifja session SESSION_ID --limit 100 --json
rifja tasks --project harbor
rifja decisions --project harbor
rifja items --project harbor --all --json
rifja search "fixture schema" --project harbor --provider codex --actor user --limit 20
rifja resume harbor
rifja explain RECORD_ID --json
```

The date range includes both endpoint calendar days. `daily` distinguishes activity on known dates from carried-over unfinished work. No activity at known timestamps does not establish that no work occurred. A `session` selector can be an application session ID or an unambiguous native session ID; the list exposes application IDs for subsequent operations.

Daily views also include cached commits on the selected day when no session records exist for that project. These are observed commits, not proof of personal authorship. Date/worktree selection precedes the display limit: 50 activity items and 50 carryover items per project are shown by default, with explicit omission counts. Increase `daily --limit` up to 1,000 or narrow the range/worktree or use tasks, session, search and explain to investigate omitted context. Git history remains the bounded history captured by repository observations, not an exhaustive historical Git archive.

`tasks` includes task candidates, next actions, blockers, corrections and claims with their statuses. It filters these categories and prioritizes unfinished work before applying the display limit. It is not exclusively a list of active work. `decisions` can include superseded historical decisions with their statuses. `--all` additionally includes archived/rejected items; cancellations are already visible. `resume` is the current continuation view and retains applicable user corrections.

Before applying its item limit, `resume` selects active blockers, next actions, tasks and decisions ahead of historical items. Its next-action section contains at most five recorded next actions or tasks, with explicit next actions first; it does not invent steps to fill the list. Recorded priority and dependency fields are available in JSON. An overall objective appears only when explicitly recorded in supported user intent; otherwise the objective remains unknown.

`search` is literal local full-text search. Its optional project/provider/actor filters narrow the recorded evidence, not its authority. Use `explain` with a result's `record_id`/record `id` to inspect its provider, original timestamp, source location, source generation and availability. All `--limit` options accept 1–1000; views disclose omissions where applicable.

For a single worktree, copy its ID from `project show --json`:

```sh
rifja resume harbor --worktree WORKTREE_ID
rifja daily 2026-09-05 --project harbor --worktree WORKTREE_ID
rifja export harbor --worktree WORKTREE_ID --format markdown
```

Those filters also accept a registered worktree path. `resume` normally observes current Git; `--cached` deliberately uses the stored observation. Freshness of Git and freshness of imported sessions are separate: run `refresh` to update sessions. A past passing test result and an agent's “done” claim do not verify the current revision.

Explicit `GOAL:`, `OBJECTIVE:`, `HEDEF:` and `AMAÇ:` user statements remain available independently of the recent-record window. The latest active objective in the selected scope is shown; it can be corrected/cancelled through `memory correct` like other extracted items. Quoted objectives and assistant proposals do not become authoritative objectives.

## Record useful intent and correct it

The extractor recognizes explicit English/Turkish task, next-action, blocker and decision markers, plus supported direct requests. For example, a source conversation can contain:

```text
TASK: Repair empty-date import handling.
BLOCKER: Critical: the sample CSV is missing.
NEXT: Locate the sample CSV before changing the parser.
DECISION: Store empty dates as null.
GÖREV: Boş tarih alanını destekle.
ENGEL: Kritik: örnek CSV dosyası eksik.
SONRA: Önce örnek dosyayı bul.
KARAR: Boş tarih null olarak saklanacak.
```

These are examples of conversation content, not shell commands. Quoted/code examples do not become active work. Unknown prose stays unclassified; this release does not claim general natural-language understanding. Explicit cancellation/supersession references preserve their source, and assistant success language remains a claim.

Classification reads the full redacted record within the provider's input-size limit, while stored/displayed source excerpts remain bounded to 4,096 characters. Explicit actions after the excerpt boundary are retained as separate bounded candidates. Changes beyond that boundary receive distinct evidence identities. Known `PRIORITY:`, `DEPENDS_ON:` and `RATIONALE:` attributes survive readable resume and Markdown/JSON export; they remain recorded constraints, not executable instructions. A record with more than 256 extracted candidates is diagnosed as `record_extraction_limit`; its excerpt remains inspectable, candidate expansion is rejected, and source coverage remains partial. Split such structured input into smaller records before importing.

For a durable correction through the CLI, use the application item ID or record ID shown by `items --json`:

```sh
rifja memory correct ITEM_ID --status cancelled --reason "The replacement is no longer needed."
rifja memory correct ITEM_ID --text "Validate the new CSV format only." --reason "Scope narrowed."
rifja memory correct ITEM_ID --status user_completed --reason "I completed this task; verification is recorded separately."
```

User-reported completion is distinct from independently verified code. A correction is application memory; it does not rewrite the transcript. Corrections survive source rebuilds.

If a record has missing or incorrect working context, associate it explicitly:

```sh
rifja project associate RECORD_ID harbor --worktree WORKTREE_ID --reason "This session was recorded in this checkout."
```

The target may also be an application session ID to associate its records. `--worktree` for this command requires an ID belonging to the selected project. This establishes context, not authorship of Git changes.

## Maintain facts and an engineering constitution

```sh
rifja memory add fact "The local fixture set is the acceptance reference." --scope harbor --ref RECORD_ID --reason "User-confirmed project context."
rifja memory add principle "Capture revision-specific evidence before accepting a fix." --origin inferred --ref RECORD_ID --reason "Proposed lesson from the recorded work."
rifja memory list --kind principle --json
rifja constitution --project harbor --json
rifja constitution --project harbor --accepted
```

`memory add` supports `principle`, `fact`, `decision`, `task` and `context`. Scope defaults to `global`; a project name or ID selects project scope. Repeat `--ref` to attach multiple evidence records. Entries with `--origin inferred` begin as `proposed`. Explicit entries begin as `accepted`; choose the inferred workflow when you want review before adoption. Acceptance preserves an inferred entry's origin instead of relabeling it explicit.

`resume` includes accepted global and selected-project principles and other accepted memory. Accepted facts, decisions, tasks and context appear as local memory, separately from extracted transcript candidates. These entries also participate in bounded exports; proposed, rejected and superseded entries are excluded. Memory scope is global or project-wide, so a worktree filter still includes applicable project memory. Inspect all entries with `memory list --json` and principles with `constitution --project harbor --json`.

```sh
rifja memory edit PRINCIPLE_ID --status accepted --reason "Reviewed and adopted." --exceptions "Exploratory spikes." --conflicts "Document unresolved competing requirements here."
rifja memory edit NEW_PRINCIPLE_ID --supersedes OLD_PRINCIPLE_ID --reason "The new rule is more precise."
rifja memory edit PRINCIPLE_ID --status rejected --reason "Insufficient evidence."
```

Supersession marks the older entry `superseded` and links it from the newer entry. Exceptions, conflicts and reasons remain part of the stored principle; they do not grant permissions to execute source instructions. Resume and both export formats carry a principle's exceptions and conflicts with its rule. A bounded export omits the whole principle and increments its omission count if that combined entry cannot fit. Inspect `constitution --json` for the full stored entry, including its reason and supersession link. Supported editing statuses are `active`, `proposed`, `accepted`, `rejected`, `superseded`, `cancelled`, `archived`, `user_completed`, `claimed_complete`, `blocked`, `abandoned` and `pending`.

## Export a bounded handoff

```sh
rifja export harbor --format markdown --max-chars 24000 --output "./harbor-handoff.md"
rifja export harbor --format json --max-chars 24000 --output "./harbor-handoff.json"
rifja export harbor --format json --cached
```

Without `--output`, export writes the document to stdout. With `--output`, the parent directory must exist and the destination must be new. Output files are created with private permissions. The character budget accepts 2000–1,000,000 and excludes the final newline. The exporter either fits bounded context with omission notices or reports that required metadata cannot fit. Inspect omitted context and retrieve more with `resume`, `items`, `session` or `explain` before acting.

The bounded document contains project identity, generation time, known objective, coverage, Git observations, accepted principles/local memory and evidence-linked items as space permits. Current location and accepted memory are considered before imported items; blockers and next actions lead the imported-item selection. Omissions are counted separately for worktrees, principles, local memory, items and uncertainty. A positive count means material context is missing, even if the export command succeeds. Source references are local identifiers and may be unavailable to the recipient; an export is not a backup of its evidence.

The JSON export is a document with `kind: "context_export"`. Adding the global `--json` flag to an export printed to stdout wraps that document text in the normal CLI response's `data` string; omit global `--json` when you want the JSON export document directly. Export selects the entire project unless `--worktree WORKTREE_ID` or a registered worktree path narrows it.

## Serve coding agents (MCP over stdio)

```sh
rifja mcp
```

`rifja mcp` runs the agent-facing MCP tool server speaking newline-delimited JSON-RPC 2.0 on stdin/stdout. Read tools (`status`, `projects`, `search`, `resume`, `tasks`, `daily`, `explain`, `memory`) and operational tools (`setup`, `register_source`, `register_project`, `refresh`, `remember`, `associate`) cover the same local state the CLI uses, with the same provenance and bounded excerpts. Every call is recorded in the activity log. Agents may only propose memory entries; destructive operations (forget, retention, backup, restore) are never exposed to agents. There is no TCP listener and no daemon: an agent host spawns the server, and it ends when stdin closes. Per-request failures — writer contention, invalid selectors — come back as MCP `isError` results with stable codes and retry guidance; the server never exits on them. See [agent ecosystem integrations](integrations.md) for Claude Code, Codex and Hermes configuration, hook snippets and the data-only injection rules.

## Optional local dashboard

```sh
rifja ui
rifja ui --port 42970 --open
```

`rifja ui` serves a read-only dashboard on `127.0.0.1` (default port 41970, overridable with `--port`; a taken port is an error, never a silent fallback). It prints a one-time sign-in URL: the first open exchanges the embedded token for an `HttpOnly` + `SameSite=strict` session cookie and invalidates the token, so the link cannot be replayed. Every page — static assets included — requires that session, responses carry `Cache-Control: no-store` and a restrictive CSP, and the browser is opened only with `--open`. The dashboard is GET-only: there are no mutation endpoints, so `refresh`, registration and forgetting stay CLI operations. Pages (overview, timeline, sessions, search, evidence, memory, sources) render the same `App` data as the CLI, with transcript-derived content escaped and no inline script. Stop with Ctrl-C; the server exists only while the command runs. The trust boundary is documented in [the threat model](rifja-threat-model.md).

Document excerpts in JSON can share provenance through `evidence.observation_ref`.
Resolve that key in `document_observations` for the observation time, Git revision,
working-tree scope, modification status and worktree ID. Path, line references,
record identity and evidence availability remain on each excerpt. These references
are contained in the export; resolving them does not require opening local files.

## Backup, restore and migrate

```sh
rifja backup "/path/to/backups/continuity.sqlite3"
rifja --home "/path/to/restored state" restore "/path/to/backups/continuity.sqlite3"
rifja --home "/path/to/restored state" doctor
```

Backups are consistent SQLite snapshots containing imported state, configuration, durable memory, corrections and forget rules. The backup destination must be new; parent directories are created when necessary. Producer sources and Git repositories are not included.

Restore requires a nonexistent or empty destination directory. Do not run `setup` there first, because setup creates application state. A nonempty destination is rejected and preserved. After restoring, select the restored home explicitly and inspect source paths/coverage before refreshing, especially on a different machine.

Application schemas 1 and 2 migrate to schema 3, and schema 3 migrates to schema 4, when opened. A private `before-migration-vN.sqlite3` backup is created before each migration. Schema 3 adds a covering index for daily queries; schema 4 adds the append-only `activity` record that powers the dashboard's agent-activity view. Newer state schemas are refused. Restore validates supported backup versions and structural/integrity checks before replacing the empty destination. Use a compatible program version or a compatible backup in a separate home; do not manually lower a database's schema version.

To upgrade a Homebrew installation, run `brew update` and `brew upgrade 0merUfuk/rifja/rifja`, then `refresh` against the same application state. Homebrew manages the environment. A version change replays derived extraction once; durable memory is retained. Release 0.1.0rc2 introduced schema 3. Overrides on unchanged pre-rc2 long records are carried to the new full-text evidence identity when the matching prior item is unambiguous; old evidence references remain available as history. A concurrently changed source is not assumed to be the same evidence.

## Retention, forgetting and uninstalling

```sh
rifja retention --before 2026-08-01
rifja retention --before 2026-08-01 --confirm
rifja session --json
rifja forget SESSION_ID --confirm
```

Retention without `--confirm` is a dry run. It selects whole sessions whose last known event is before the cutoff in the configured timezone, excluding sessions with unresolved event times. Confirmation removes those sessions. `forget` requires an application session ID from `session --json` and explicit `--confirm`.

Forgetting removes associated imported records and search entries and stores a provider/session rule preventing reimport. `refresh --rebuild` preserves these rules and durable user memory. It does not undo forgetting. References from retained memory show missing evidence. Producer transcripts remain untouched; earlier backups and exports remain independent copies. No forensic erasure is promised.

For the recommended Homebrew installation, uninstall the application with:

```sh
brew uninstall --force 0merUfuk/rifja/rifja
```

Application state, exports, backups and producer transcripts remain. A new `--home` provides fresh application state without deleting the old one. Deleting a state directory also deletes its memory, configuration and rules preventing reimport; a later fresh import can then bring those sessions back.

## Automation, output and diagnostics

Global `--json`, `--home PATH` and `--quiet` can appear before or after subcommands. Successful ordinary JSON responses have `schema_version: 1`, `command` and `data`. Human-readable output is plain text/Markdown with terminal control sequences removed; every command renders short lines in human mode, and JSON remains available for all of them with `--json`.

Errors keep their exit codes in both modes. Human mode writes the contract label plus next-action hints to stderr:

```text
Error: project_not_found
  - Registered projects: `rifja project list`.
  - Register one: `rifja project add PATH`.
```

In `--json` mode the error is a versioned envelope on stdout, so automation reads machine-readable hints:

```json
{"schema_version":1,"command":"resume","error":{"code":"project_not_found","hints":["Registered projects: `rifja project list`.","Register one: `rifja project add PATH`."]}}
```

`hints` is empty for labels without a specific next action. `code` is the contract label (or the exception class name for local I/O/database errors, and `busy` for writer contention). Parse-level usage errors such as an unknown command or a malformed flag remain argparse text on stderr with exit 2. A bare `rifja` invocation prints an overview with grouped commands and exits 2; with `--json` it emits the error envelope with code `no_command`.

Resume and handoff Markdown escape source Markdown/HTML syntax and display embedded newlines as `↵`, preserving the boundary between document structure and imported text. JSON keeps structured text fields for machine use. These presentation controls do not make source content authoritative or safe to execute.

| Exit | Meaning | Next step |
| --- | --- | --- |
| 0 | Command completed | Inspect returned coverage and evidence limits where relevant. |
| 2 | Invalid input or local operation error | Read the error hints (stderr in human mode, `error.hints` in `--json`); correct the path, selector, value or state issue. |
| 3 | Refresh completed with partial coverage | Inspect `source list --json`, restore available inputs and retry. |
| 4 | Application writer is busy | Wait for the other writer, then retry (`error.code` is `busy` in `--json`). |
| 130 | Interrupted | Committed state is retained; retry refresh. |

There is no background monitor, network upload, provider write-back or implicit transcript scan. Schedule explicit commands in your own environment if needed. Keep source roots narrow and use a separate application state directory for experiments.

## Privacy and support limits

The reader uses bounded excerpts, best-effort secret redaction, private state/export permissions and source-generation provenance. It skips sensitive/generated paths and does not follow user-controlled source symlink traversal. These controls are not encryption or a guarantee of complete secret detection. Redaction can shorten or alter a source excerpt; consult its evidence status before relying on it. Review exported material before sharing it.

The program observes Git metadata and reads configured transcript stores. It does not edit repository contents, launch producer agents, checkpoint producer databases, fetch remotes or execute commands copied from a transcript. Imported text remains untrusted context.

For provider changes or missing coverage, consult [supported formats and known gaps](providers.md). For what has been asserted by the synthetic corpus, consult [semantic acceptance](acceptance.md); release-specific execution evidence must accompany broader compatibility or performance claims.

## Bounded project documents (rc3)

Use `document add PROJECT` to opt in before refresh. See [project context](project-context.md) for allowlists, worktree selection, provenance, ordinary status language, identity and output limits. Rc3 retains schema 3 and automatically rebuilds its derived extraction on upgrade; document claims never become freshly executed verification.

## Existing installations

Rifja reuses the former application’s state in place. The old command and home
environment variable remain compatibility aliases. See [identity migration](identity.md)
for exact precedence and Homebrew upgrade instructions.
