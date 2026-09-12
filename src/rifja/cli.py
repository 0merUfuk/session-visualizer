"""CLI boundary: stable exit codes, versioned JSON, no implicit source scan."""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .app import App
from .ingest import Ingestor
from .render import bounded_export, readable, safe_output
from .store import SCHEMA_VERSION, BusyError, Store, restore


def default_home() -> Path:
    for variable in ("RIFJA_HOME", "SESSION_VISUALIZER_HOME"):
        if os.environ.get(variable):
            return Path(os.environ[variable]).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        current, legacy = base / "Rifja", base / "SessionVisualizer"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        current, legacy = base / "rifja", base / "session-visualizer"
    # Reuse existing state in place: moving SQLite/WAL files could race an older CLI.
    current_exists = current.exists() or current.is_symlink()
    legacy_exists = legacy.exists() or legacy.is_symlink()
    if current_exists and legacy_exists and current.resolve() != legacy.resolve():
        raise ValueError("multiple_default_state_directories_select_one_with_--home_or_RIFJA_HOME")
    return legacy if legacy_exists and not current_exists else current


def detect_timezone() -> tuple[str, str]:
    """Resolve the machine zone: TZ, then /etc/localtime, then the UTC fallback.

    Every step is validated as an IANA key. A set-but-invalid TZ value ends the
    chain; human output uses the returned origin to say whether the zone was
    explicit, detected or a fallback, so UTC is never applied silently.
    """
    raw = os.environ.get("TZ", "").strip()
    candidate = raw.removeprefix(":")
    if candidate:
        try:
            ZoneInfo(candidate)
        except ValueError, ZoneInfoNotFoundError:
            return "UTC", "fallback"
        return candidate, "detected"
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC", "fallback"
    marker = "/zoneinfo/"
    index = target.rfind(marker)
    if index < 0:
        return "UTC", "fallback"
    key = target[index + len(marker) :]
    try:
        ZoneInfo(key)
    except ValueError, ZoneInfoNotFoundError:
        return "UTC", "fallback"
    return key, "detected"


COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Setup", ("init", "setup", "config", "doctor")),
    ("Data", ("source", "document", "project", "refresh", "retention", "forget")),
    (
        "Inspect",
        ("daily", "resume", "session", "tasks", "decisions", "items", "search", "explain"),
    ),
    ("Deliver", ("export", "memory", "constitution", "backup", "restore", "mcp", "ui")),
)


class GroupedHelpParser(argparse.ArgumentParser):
    """Root-only help formatting: commands grouped by workflow instead of one list.

    Subparsers are created with parser_class=argparse.ArgumentParser so leaf
    commands keep the stock formatter; this class must never be reached without
    a subparsers action.
    """

    def format_help(self) -> str:
        text = super().format_help()
        lines = text.splitlines()
        try:
            head = lines.index("positional arguments:")
            tail = next(i for i in range(head + 1, len(lines)) if lines[i] == "options:")
            subparsers = next(
                action for action in self._actions if isinstance(action, argparse._SubParsersAction)
            )
        except ValueError, StopIteration:
            return text
        # The per-command help strings live on the subparsers action, not the parsers.
        helps = {
            item.dest: str(item.help or "").strip()
            for item in getattr(subparsers, "_choices_actions", [])
        }
        grouped: list[str] = []
        for title, names in COMMAND_GROUPS:
            grouped.append(title + ":")
            width = max(len(name) for name in names)
            grouped += [f"  {name:<{width}}  {helps.get(name, '')}".rstrip() for name in names]
            grouped.append("")
        return "\n".join([*lines[:head], "commands:", *grouped, *lines[tail:]])


def overview_text() -> str:
    lines = [
        "Your coding agents delete their own history. Rifja keeps it — offline, with receipts.",
        "",
        "Offline, evidence-aware continuity for local coding-agent sessions and Git.",
        "",
        "commands:",
    ]
    for title, names in COMMAND_GROUPS:
        lines.append(f"  {title}: " + ", ".join(names))
    lines += [
        "",
        "Start here:",
        "  rifja init                         guided setup (confirms every step)",
        "  rifja setup                        non-interactive setup (timezone is detected)",
        "  rifja project add PATH             register a repository to resume",
        "  rifja source add PROVIDER PATH     register producer transcripts or databases",
        "  rifja refresh                      import configured sources",
        "  rifja resume PROJECT               pick up where a session left off",
        "",
        "Run `rifja COMMAND --help` for details. Nothing is read before it is registered.",
    ]
    return "\n".join(lines)


ERROR_HINTS: dict[str, tuple[str, ...]] = {
    "busy": ("Wait for the running refresh, backup or restore, then retry.",),
    "no_command": (
        "Run `rifja --help` for the grouped command list.",
        "Initialize state with `rifja setup`, then register `rifja project add PATH`.",
    ),
    "unknown_command": ("Run `rifja --help` for the grouped command list.",),
    "project_not_found": (
        "Registered projects: `rifja project list`.",
        "Register one: `rifja project add PATH`.",
    ),
    "ambiguous_project_name_use_id": ("Use the project ID from `rifja project list`.",),
    "session_not_found_or_ambiguous": (
        "List sessions and copy an application ID: `rifja session --json`.",
    ),
    "session_not_found_or_already_forgotten": (
        "List sessions and copy an application ID: `rifja session --json`.",
    ),
    "worktree_not_found_or_wrong_project": (
        "Copy a worktree ID from `rifja project show PROJECT --json`.",
    ),
    "worktree_does_not_belong_to_project": (
        "Copy a worktree ID from `rifja project show PROJECT --json`.",
    ),
    "select_document_worktree_explicitly": (
        "Add `--worktree WORKTREE_ID` (IDs: `rifja project show PROJECT --json`).",
    ),
    "document_requires_registered_active_worktree": (
        "Register the repository first: `rifja project add PATH`.",
    ),
    "document_worktree_unavailable": (
        "Re-register the checkout at its current path: `rifja project add PATH`.",
    ),
    "source_requires_existing_non_symlink_path": (
        "Register an existing file or directory: `rifja source add PROVIDER PATH`.",
        "Check candidates: `rifja source discover`.",
    ),
    "source_is_excluded": ("Review the patterns in `rifja config exclusions`.",),
    "unknown_provider": ("PROVIDER is one of: codex, claude, hermes.",),
    "repository_root_must_not_be_symlink": (
        "Register the real checkout path instead of the symlink.",
    ),
    "repository_unavailable_or_untrusted": (
        "Register an existing Git working tree: `rifja project add PATH`.",
    ),
    "multiple_default_state_directories_select_one_with_--home_or_RIFJA_HOME": (
        "Pass `--home PATH`, or set RIFJA_HOME to the intended state directory.",
    ),
    "unknown_configuration_key": ("Supported keys: timezone, roots, exclusions.",),
    "configuration_requires_string_list": (
        "Pass a JSON list of strings, e.g. '[\"*/scratch/*\"]'.",
    ),
    "limit_must_be_1_to_1000": ("Pass --limit between 1 and 1000.",),
    "search_requires_1_to_500_characters": ("Search 1-500 characters.",),
    "export_destination_exists": (
        "Choose a new --output path; existing files are never overwritten.",
    ),
    "export_budget_must_be_2000_to_1000000_characters": (
        "Pass --max-chars between 2000 and 1000000.",
    ),
    "export_budget_too_small_for_required_metadata": (
        "Raise --max-chars; the required metadata alone exceeds the budget.",
    ),
    "invalid_memory_kind_or_origin": (
        "kind: principle|fact|decision|task|context; --origin: explicit|inferred.",
    ),
    "memory_reference_must_be_known_record_id": (
        "Copy a record ID from `rifja explain RECORD_ID --json`.",
    ),
    "inferred_memory_must_start_proposed": (
        "Accept it afterwards: `rifja memory edit ID --status accepted --reason ...`.",
    ),
    "invalid_status": (
        (
            "Statuses: active, proposed, accepted, rejected, superseded, cancelled, archived, "
            "user_completed, claimed_complete, blocked, abandoned, pending."
        ),
    ),
    "memory_not_found": ("List IDs: `rifja memory list --json`.",),
    "superseded_memory_not_found": ("List IDs: `rifja memory list --json`.",),
    "correction_target_not_found_use_item_or_record_id": (
        "Copy an item or record ID from `rifja items --json`.",
    ),
    "correction_requires_text_or_status": ("Pass --text and/or --status together with --reason.",),
    "association_target_not_found_use_record_or_session_id": (
        "Copy an ID from `rifja session --json` or `rifja items --json`.",
    ),
    "forget_requires_--confirm; backups_and_exports_are_separate_copies": (
        "Repeat with --confirm to remove the session and prevent reimport.",
    ),
    "backup_destination_exists": (
        "Choose a new backup path; existing files are never overwritten.",
    ),
    "invalid_backup_path": ("Pass the path of a `rifja backup` snapshot file.",),
    "restore_requires_empty_destination; existing state is preserved": (
        "Point --home at a new or empty directory; existing state is never replaced.",
    ),
    "unsupported_backup_schema": (
        "Restore with a program version that supports this backup schema.",
    ),
    "newer_schema": (
        "Use a program version that supports this state schema; the state was left untouched.",
    ),
    "database_must_not_be_symlink": ("Point --home at a real directory, not a symlink.",),
    "lock_must_not_be_symlink": ("Remove the symlinked refresh.lock inside the state directory.",),
    "source_discovery_limit": ("Register narrower source roots instead of one huge directory.",),
    "source_changed_during_read": (
        "Re-run `rifja refresh`; the source changed while it was read.",
    ),
    "document_changed_during_read": (
        "Re-run `rifja refresh`; a document changed while it was read.",
    ),
    "ui_port_unavailable_pass_--port": (
        "Pass --port with a free port; the dashboard never falls back to another port silently.",
    ),
    "ui_port_out_of_range": ("Pass --port between 1 and 65535.",),
}


def error_hints(label: str) -> list[str]:
    """Next actions for a contract label; exact match first, then the longest prefix."""
    if label in ERROR_HINTS:
        return list(ERROR_HINTS[label])
    for key in sorted(ERROR_HINTS, key=len, reverse=True):
        if label.startswith(key):
            return list(ERROR_HINTS[key])
    return []


def _decorated(stream: Any) -> bool:
    """Terminal decoration is only for interactive, color-capable streams."""
    return (
        hasattr(stream, "isatty")
        and stream.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )


_ATTENTION_STATUSES = frozenset({"partial", "failed", "missing", "unsupported", "unavailable"})


class RefreshProgress:
    """Render ingestor events as bounded stderr progress; never blocks a refresh.

    The ingestor contract is observe-only: this callback must not raise or
    mutate state, so any failure disables further output instead of stopping
    the refresh. Interactive terminals get one line updated in place; piped
    runs get plain appended lines at a bounded cadence.
    """

    _INTERVAL = 0.25

    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.attention: list[dict[str, str]] = []
        self._stream = sys.stderr
        self._tty = _decorated(self._stream)
        self._drawn = False
        self._last = 0.0
        self._width = 0

    def __call__(self, event: dict[str, Any]) -> None:
        if self.quiet:
            return
        try:
            self._event(event)
        except Exception:  # noqa: BLE001 - progress is best-effort; never stop a refresh.
            self.quiet = True

    def _event(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "source" or (kind == "root" and event.get("status") == "unavailable"):
            self._watch(event)
        if kind not in {"source", "progress"}:
            return
        now = time.monotonic()
        if not self._drawn or now - self._last >= self._INTERVAL:
            self._drawn = True
            self._last = now
            self._write(self._line(event))

    def _watch(self, event: dict[str, Any]) -> None:
        if event.get("status") in _ATTENTION_STATUSES and len(self.attention) < 50:
            self.attention.append(
                {
                    "provider": str(event.get("provider", "unknown")),
                    "path": str(event.get("path", "")),
                    "status": str(event["status"]),
                }
            )

    def _line(self, event: dict[str, Any]) -> str:
        done = event.get("done", "?")
        counts = f"parsed {event['parsed']}, inserted {event['inserted']}"
        if event["type"] == "source":
            return f"refresh: {done} source{'s' if done != 1 else ''}; {counts} [{event['status']}] {event['path']}"
        return f"refresh: {done} source{'s' if done != 1 else ''}; {counts} — {event['path']}"

    def _write(self, line: str) -> None:
        if self._tty:
            # Plain-space padding clears the previous line without terminal sequences.
            self._stream.write("\r" + line + " " * max(0, self._width - len(line)))
            self._width = len(line)
        else:
            self._stream.write(line + "\n")
        self._stream.flush()

    def finish(self, report: dict[str, Any]) -> None:
        if self.quiet or not self._drawn:
            return
        line = (
            f"refresh: {report['status']} — "
            f"{report['sources']} source{'s' if report['sources'] != 1 else ''} "
            f"({report['unchanged']} unchanged), {report['parsed_records']} parsed, "
            f"{report['inserted_records']} inserted, {report['partial']} partial, "
            f"{report['failed']} failed, {report['missing']} missing"
        )
        if self._tty:
            self._write(line)
            self._stream.write("\n")
        else:
            self._write(line)
        self._drawn = False


def parser() -> argparse.ArgumentParser:
    p = GroupedHelpParser(
        description="Offline evidence-aware continuity for local sessions and Git.",
        epilog="Global flags --home PATH, --json and --quiet work before or after subcommands. Exit: 0 success, 2 input/error, 3 partial refresh, 4 busy, 130 interrupted.",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--home", type=Path, help="Private application state directory")
    p.add_argument("--json", action="store_true", help="Versioned JSON output")
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress refresh progress output")
    sub = p.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        parser_class=argparse.ArgumentParser,
    )
    init = sub.add_parser("init", help="Guided first-run setup (interactive terminal)")
    init.add_argument(
        "--timezone", help="IANA zone; detected from TZ or /etc/localtime when omitted"
    )
    setup = sub.add_parser("setup", help="Initialize local configuration")
    setup.add_argument(
        "--timezone", help="IANA zone; detected from TZ or /etc/localtime when omitted"
    )
    config = sub.add_parser("config", help="View or edit configuration")
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?", help="Value; lists use JSON")
    source = sub.add_parser("source", help="Discover, register or inspect session sources")
    ss = source.add_subparsers(dest="action", required=True)
    ss.add_parser("discover")
    ss.add_parser("list")
    sa = ss.add_parser("add")
    sa.add_argument("provider", choices=["codex", "claude", "hermes"])
    sa.add_argument("path", type=Path)
    document = sub.add_parser("document", help="Opt in to bounded registered-project documents")
    ds = document.add_subparsers(dest="action", required=True)
    ds.add_parser("list")
    da = ds.add_parser("add")
    da.add_argument("project")
    da.add_argument("--worktree")
    da.add_argument("--include", action="append", dest="patterns")
    da.add_argument("--max-files", type=int, default=24)
    da.add_argument("--max-bytes", type=int, default=131072)
    da.add_argument("--max-depth", type=int, default=2)
    refresh = sub.add_parser("refresh", help="Incrementally import configured sources")
    refresh.add_argument(
        "--verify", action="store_true", help="Rehash inputs even when metadata is unchanged"
    )
    refresh.add_argument(
        "--rebuild",
        action="store_true",
        help="Replay sources, preserving user memory and provenance",
    )
    project = sub.add_parser("project", help="Register, discover and inspect repositories")
    ps = project.add_subparsers(dest="action", required=True)
    pa = ps.add_parser("add")
    pa.add_argument("path", type=Path)
    pa.add_argument("--name")
    pd = ps.add_parser("discover")
    pd.add_argument("roots", type=Path, nargs="+")
    ps.add_parser("list")
    pi = ps.add_parser("show")
    pi.add_argument("project")
    pi.add_argument("--observe", action="store_true", help="Inspect current Git state")
    assoc = ps.add_parser("associate")
    assoc.add_argument("target", help="Record or session ID")
    assoc.add_argument("project")
    assoc.add_argument("--worktree")
    assoc.add_argument("--reason", required=True)
    daily = sub.add_parser("daily", help="Daily or date-range activity by project")
    daily.add_argument("date", nargs="?")
    daily.add_argument("--to")
    daily.add_argument("--project")
    daily.add_argument("--worktree")
    daily.add_argument(
        "--limit", type=int, default=50, help="Activity/carryover items per project (1-1000)"
    )
    resume = sub.add_parser("resume", help="Current Git plus evidence-linked continuation context")
    resume.add_argument("project")
    resume.add_argument("--worktree")
    resume.add_argument("--cached", action="store_true", help="Use cached Git observations")
    resume.add_argument("--limit", type=int, default=50)
    session = sub.add_parser("session", help="Inspect a session or list known sessions")
    session.add_argument("session", nargs="?")
    session.add_argument("--limit", type=int, default=50)
    for command, help_text in [
        ("tasks", "Inspect unfinished work and task claims"),
        ("decisions", "Inspect explicit decisions"),
        ("items", "Inspect all derived evidence items"),
    ]:
        items = sub.add_parser(command, help=help_text)
        items.add_argument("--project")
        items.add_argument("--limit", type=int, default=100)
        items.add_argument("--all", action="store_true")
    search = sub.add_parser("search", help="Local full-text search with provenance")
    search.add_argument("query")
    search.add_argument("--project")
    search.add_argument("--provider", choices=["codex", "claude", "hermes"])
    search.add_argument("--actor")
    search.add_argument("--limit", type=int, default=20)
    explain = sub.add_parser("explain", help="Inspect evidence origin and current source status")
    explain.add_argument("record")
    export = sub.add_parser("export", help="Bounded Markdown or JSON handoff context")
    export.add_argument("project")
    export.add_argument("--worktree")
    export.add_argument("--format", choices=["markdown", "json"], default="markdown")
    export.add_argument("--max-chars", type=int, default=24000)
    export.add_argument("--output", type=Path)
    export.add_argument("--cached", action="store_true")
    memory = sub.add_parser(
        "memory", help="Durable facts, corrections and engineering constitution"
    )
    ms = memory.add_subparsers(dest="action", required=True)
    ml = ms.add_parser("list")
    ml.add_argument("--kind")
    ma = ms.add_parser("add")
    ma.add_argument("kind", choices=["principle", "fact", "decision", "task", "context"])
    ma.add_argument("text")
    ma.add_argument("--scope", default="global")
    ma.add_argument("--origin", choices=["explicit", "inferred"], default="explicit")
    ma.add_argument("--ref", action="append", default=[])
    ma.add_argument("--reason")
    mu = ms.add_parser("edit")
    mu.add_argument("id")
    mu.add_argument("--status")
    mu.add_argument("--text")
    mu.add_argument("--reason", default="User edit")
    mu.add_argument("--supersedes")
    mu.add_argument("--exceptions")
    mu.add_argument("--conflicts")
    mc = ms.add_parser("correct")
    mc.add_argument("target")
    mc.add_argument("--status")
    mc.add_argument("--text")
    mc.add_argument("--reason", required=True)
    constitution = sub.add_parser(
        "constitution", help="View/export accepted and proposed engineering principles"
    )
    constitution.add_argument("--project")
    constitution.add_argument("--accepted", action="store_true")
    sub.add_parser("doctor", help="Check local state, sources and integrity")
    backup = sub.add_parser(
        "backup", help="Consistent private snapshot including user memory/config"
    )
    backup.add_argument("destination", type=Path)
    rs = sub.add_parser("restore", help="Restore a snapshot into an empty --home directory")
    rs.add_argument("backup", type=Path)
    forget = sub.add_parser("forget", help="Remove a session and prevent reimport")
    forget.add_argument("session")
    forget.add_argument("--confirm", action="store_true")
    retention = sub.add_parser("retention", help="Preview or apply historical session forgetting")
    retention.add_argument("--before", required=True)
    retention.add_argument("--confirm", action="store_true")
    sub.add_parser(
        "mcp",
        help="MCP tool server for agents over stdio: queries + setup/refresh (no network)",
    )
    ui = sub.add_parser("ui", help="Read-only local dashboard on loopback (opt-in)")
    ui.add_argument(
        "--port", type=int, default=41970, help="TCP port on 127.0.0.1 (refuses taken ports)"
    )
    ui.add_argument("--open", action="store_true", help="Open the printed URL in a browser")
    return p


def execute(args: argparse.Namespace, store: Store) -> tuple[Any, int, dict[str, Any]]:
    result, code, extra = _execute(args, store)
    # Empty state orients toward the guided path; JSON payloads stay untouched.
    if (
        args.command
        in {"source", "project", "session", "daily", "search", "tasks", "decisions", "items"}
        and store.config("timezone") is None
    ):
        extra["uninitialized"] = True
    return result, code, extra


def _execute(args: argparse.Namespace, store: Store) -> tuple[Any, int, dict[str, Any]]:
    app = App(store)
    cmd = args.command
    result: Any
    if hasattr(args, "limit") and not 1 <= args.limit <= 1000:
        raise ValueError("limit_must_be_1_to_1000")
    if cmd == "init":
        from .onboarding import run_init

        # --json callers get the plan, never an interactive session on stdout.
        return run_init(
            app,
            store,
            sys.stdout,
            timezone_arg=args.timezone,
            interactive=sys.stdin.isatty() and not args.json,
            quiet=args.quiet,
        )
    if cmd == "setup":
        zone, origin = (args.timezone, "explicit") if args.timezone else detect_timezone()
        return app.setup(zone), 0, {"timezone_origin": origin}
    if cmd == "config":
        if args.value is None:
            return (
                {
                    r["key"]: json.loads(r["value"])
                    for r in store.rows("SELECT * FROM config")
                    if args.key is None or r["key"] == args.key
                },
                0,
                {},
            )
        value = args.value if args.key == "timezone" else json.loads(args.value)
        return app.configure(args.key, value), 0, {}
    if cmd == "source":
        if args.action == "discover":
            return app.source_discover(), 0, {}
        if args.action == "add":
            return app.source_add(args.provider, args.path), 0, {}
        return (
            {
                "configured": store.config("sources", []),
                "coverage": app.coverage(limit=None),
            },
            0,
            {},
        )
    if cmd == "refresh":
        progress = RefreshProgress(args.quiet)
        result = Ingestor(store).refresh(args.verify, args.rebuild, progress=progress)
        progress.finish(result)
        extra = {"attention": progress.attention}
        return result, 0 if result["status"] == "passed" else 3, extra
    if cmd == "document":
        if args.action == "list":
            return (
                {
                    "configured": store.config("project_documents", []),
                    "coverage": app.coverage(limit=None),
                },
                0,
                {},
            )
        from .documents import configure

        pid = app.find_project(args.project)["id"]
        if args.worktree:
            wid = app.find_worktree(args.worktree, pid)
        else:
            trees = store.rows("SELECT id FROM worktrees WHERE project_id=? AND active=1", (pid,))
            if len(trees) != 1:
                raise ValueError("select_document_worktree_explicitly")
            wid = trees[0]["id"]
        return (
            configure(
                store, pid, wid, args.patterns, args.max_files, args.max_bytes, args.max_depth
            ),
            0,
            {},
        )
    if cmd == "project":
        if args.action == "add":
            return app.register(args.path, args.name), 0, {}
        if args.action == "discover":
            return app.discover(args.roots), 0, {}
        if args.action == "show":
            return app.project(args.project, args.observe), 0, {}
        if args.action == "associate":
            return app.associate(args.target, args.project, args.worktree, args.reason), 0, {}
        return {"projects": store.rows("SELECT * FROM projects ORDER BY name,id")}, 0, {}
    if cmd == "daily":
        return app.daily(args.date, args.to, args.project, args.worktree, args.limit), 0, {}
    if cmd == "resume":
        return app.resume(args.project, not args.cached, args.limit, args.worktree), 0, {}
    if cmd == "session":
        return (
            (
                app.session(args.session, args.limit)
                if args.session
                else {
                    "sessions": store.rows(
                        "SELECT * FROM sessions ORDER BY id LIMIT ?", (args.limit,)
                    )
                }
            ),
            0,
            {},
        )
    if cmd in {"tasks", "decisions", "items"}:
        result = app.items(
            args.project,
            "decision" if cmd == "decisions" else None,
            args.limit,
            args.all,
            prioritize_active=cmd == "tasks",
            kinds=frozenset({"task", "next_action", "blocker", "claim", "correction"})
            if cmd == "tasks"
            else None,
        )
        return result, 0, {}
    if cmd == "search":
        return app.search(args.query, args.project, args.provider, args.actor, args.limit), 0, {}
    if cmd == "explain":
        return app.evidence(args.record), 0, {}
    if cmd == "export":
        result = bounded_export(
            app.resume(args.project, not args.cached, worktree=args.worktree),
            args.format,
            args.max_chars,
        )
        if args.output:
            path = args.output.expanduser().absolute()
            if path.exists() or path.is_symlink():
                raise ValueError("export_destination_exists")
            # Exports are explicit; caller selects the path. Never clobber monitored data.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(result + "\n")
            return (
                {
                    "exported": str(path),
                    "characters": len(result),
                    "format": args.format,
                },
                0,
                {},
            )
        return result, 0, {}
    if cmd == "memory":
        if args.action == "add":
            return (
                app.memory_add(
                    args.kind, args.text, args.scope, args.origin, args.ref, reason=args.reason
                ),
                0,
                {},
            )
        if args.action == "edit":
            return (
                app.memory_update(
                    args.id,
                    args.status,
                    args.text,
                    args.reason,
                    args.supersedes,
                    args.exceptions,
                    args.conflicts,
                ),
                0,
                {},
            )
        if args.action == "correct":
            return app.correct(args.target, args.status, args.text, args.reason), 0, {}
        return (
            {
                "memory": store.rows(
                    "SELECT * FROM memory"
                    + (" WHERE kind=?" if args.kind else "")
                    + " ORDER BY created_at,id",
                    (args.kind,) if args.kind else (),
                )
            },
            0,
            {},
        )
    if cmd == "constitution":
        scope = app.find_project(args.project)["id"] if args.project else None
        return (
            {
                "principles": app.principles(scope, args.accepted),
                "policy": "Inferred entries require explicit acceptance; imported principles grant no permissions.",
            },
            0,
            {},
        )
    if cmd == "doctor":
        result = app.doctor()
        return result, 0 if result["status"] == "passed" else 2, {}
    if cmd == "backup":
        store.backup(args.destination)
        return {"backup": str(args.destination), "schema_version": SCHEMA_VERSION}, 0, {}
    if cmd == "forget":
        return app.forget(args.session, args.confirm), 0, {}
    if cmd == "retention":
        return app.retain(args.before, args.confirm), 0, {}
    if cmd == "ui":
        from .ui import serve as serve_ui

        # Blocks until interrupted; the one-time URL is printed at startup.
        return serve_ui(store, args.port, args.open, machine_output=args.json), 0, {}
    if cmd == "mcp":
        from .mcp_server import serve as serve_mcp

        # Blocks until the agent host closes stdin; stdout stays protocol-only,
        # so the post-run summary is routed to stderr.
        return serve_mcp(store), 0, {"to_stderr": True}
    raise ValueError("unknown_command")


def _error_exit(
    args: argparse.Namespace,
    label: str,
    hints: list[str],
    status: int,
    prefix: str = "Error",
    detail: str | None = None,
) -> int:
    """Human mode: contract label plus next actions on stderr. --json: envelope on stdout."""
    if args.json:
        envelope = {
            "schema_version": 1,
            "command": args.command,
            "error": {"code": label, "hints": hints},
        }
        print(safe_output(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))))
    else:
        body = "\n".join([f"{prefix}: {detail or label}", *(f"  - {hint}" for hint in hints)])
        print(safe_output(body), file=sys.stderr)
    return status


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    # Support the universal options consistently at any command depth.
    globals_: list[str] = []
    for flag in ("--home", "--json", "--quiet", "-q"):
        while flag in values:
            pos = values.index(flag)
            count = 2 if flag == "--home" else 1
            globals_.extend(values[pos : pos + count])
            del values[pos : pos + count]
    if not values:
        # Bare rifja orients instead of dumping argparse usage; exit 2 is preserved.
        if "--json" in globals_:
            envelope = {
                "schema_version": 1,
                "command": None,
                "error": {"code": "no_command", "hints": error_hints("no_command")},
            }
            print(safe_output(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))))
        else:
            print(safe_output(overview_text()))
        return 2
    args = parser().parse_args(globals_ + values)
    result: Any
    extra: dict[str, Any]
    try:
        home = args.home or default_home()
        if args.command == "restore":
            result, code, extra = restore(args.backup, home), 0, {}
        else:
            with Store(home) as store:
                result, code, extra = execute(args, store)
        if args.json:
            text = json.dumps(
                {"schema_version": 1, "command": args.command, "data": result},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif args.command == "export" and isinstance(result, str):
            text = result
        else:
            text = readable(args.command, result, extra, _decorated(sys.stdout))
        stream = sys.stderr if extra.get("to_stderr") else sys.stdout
        print(safe_output(text), file=stream)
        return code
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("Interrupted; committed state is preserved. Retry refresh.", file=sys.stderr)
        return 130
    except BusyError as exc:
        return _error_exit(args, "busy", error_hints("busy"), 4, prefix="Busy", detail=str(exc))
    except (ValueError, OSError, sqlite3.Error, ZoneInfoNotFoundError) as exc:
        # Controlled ValueError messages contain contract labels only, never raw provider data.
        label = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return _error_exit(args, label, error_hints(label), 2)


if __name__ == "__main__":
    raise SystemExit(main())
