"""Adversarial acceptance tests through real adapters, ingestion, storage, and App.

All source transcripts, homes, repository content, and migrations are synthetic.
Assertions express the design contract rather than the implementation's choices.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rifja.app import App
from rifja.ingest import Ingestor
from rifja.privacy import MAX_DEPTH, MAX_RECORD_BYTES
from rifja.store import (
    MIGRATION_V2,
    SCHEMA_V1,
    SCHEMA_VERSION,
    BusyError,
    Store,
    restore,
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "isolated-user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Synthetic Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "synthetic@example.invalid")


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    with Store(tmp_path / "state") as store:
        application = App(store)
        application.setup("UTC")
        yield application


def message(
    native: str,
    text: str,
    *,
    session: str = "synthetic-session",
    cwd: Path | str | None = None,
    timestamp: str = "2025-01-02T12:00:00Z",
    actor: str = "user",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": actor,
        "sessionId": session,
        "uuid": native,
        "timestamp": timestamp,
        "message": {"role": actor, "content": text},
    }
    if cwd is not None:
        value["cwd"] = str(cwd)
    return value


def encoded(*records: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(value, ensure_ascii=False).encode() + b"\n" for value in records)


def source(app: App, tmp_path: Path, *records: dict[str, Any]) -> Path:
    directory = tmp_path / "sources"
    directory.mkdir(exist_ok=True)
    path = directory / "synthetic.jsonl"
    path.write_bytes(encoded(*records))
    app.source_add("claude", directory)
    return path


def records(app: App) -> list[dict[str, Any]]:
    return app.store.rows("SELECT * FROM records ORDER BY native_id")


def checkpoint(app: App) -> dict[str, Any]:
    return app.store.rows("SELECT * FROM sources")[0]


def run_git(path: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "--initial-branch=main")
    (path / "source.txt").write_text("initial\n")
    run_git(path, "add", "--", "source.txt")
    run_git(path, "commit", "-m", "Synthetic initial revision")
    return path


def test_append_incomplete_tail_retries_without_duplicates(app: App, tmp_path: Path) -> None:
    path = source(app, tmp_path, message("one", "Task: retain the first task."))
    first = Ingestor(app.store).refresh()
    assert first["status"] == "passed"
    assert first["inserted_records"] == 1
    first_checkpoint = checkpoint(app)
    tail = encoded(message("two", "Next: complete the second task."))
    with path.open("ab") as handle:
        handle.write(tail[:-3])
    partial = Ingestor(app.store).refresh()
    assert partial["status"] == "partial"
    assert len(records(app)) == 1
    assert checkpoint(app)["offset"] == first_checkpoint["offset"]
    assert checkpoint(app)["generation"] == first_checkpoint["generation"]
    unchanged_partial = Ingestor(app.store).refresh()
    assert unchanged_partial["status"] == "partial"
    with path.open("ab") as handle:
        handle.write(tail[-3:])
    complete = Ingestor(app.store).refresh()
    assert complete["status"] == "passed"
    assert len(records(app)) == 2
    assert checkpoint(app)["offset"] == path.stat().st_size
    unchanged = Ingestor(app.store).refresh()
    assert unchanged["parsed_records"] == 0
    assert unchanged["inserted_records"] == 0
    assert unchanged["unchanged"] == 1


@pytest.mark.parametrize("mutation", ["truncate", "same_size_edit", "replacement"])
def test_changed_source_generations_preserve_superseded_evidence(
    app: App,
    tmp_path: Path,
    mutation: str,
) -> None:
    first = message("one", "Task: alpha task.")
    second = message("two", "Task: extra task.")
    path = source(app, tmp_path, first, second)
    Ingestor(app.store).refresh()
    old = next(r for r in records(app) if r["native_id"].startswith("one:"))
    prior = checkpoint(app)
    replacement = encoded(message("one", "Task: bravo task."))
    if mutation == "same_size_edit":
        original_stat = path.stat()
        revised = path.read_bytes().replace(b"alpha", b"bravo")
        assert len(revised) == original_stat.st_size
        path.write_bytes(revised)
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    elif mutation == "replacement":
        other = tmp_path / "new-generation"
        other.write_bytes(replacement)
        os.replace(other, path)
    else:
        path.write_bytes(replacement)
    refreshed = Ingestor(app.store).refresh()
    assert refreshed["status"] == "passed"
    assert checkpoint(app)["generation"] == prior["generation"] + 1
    assert app.evidence(old["id"])["status"] == "missing_or_superseded"
    assert any(r["text"] == "Task: bravo task." for r in records(app))
    assert app.store.rows("SELECT status FROM generations ORDER BY number") == [
        {"status": "superseded"},
        {"status": "current"},
    ]


def test_copy_deduplicates_records_and_keeps_both_provenance_locations(
    app: App, tmp_path: Path
) -> None:
    path = source(app, tmp_path, message("one", "Task: preserve copied provenance."))
    Ingestor(app.store).refresh()
    shutil.copy2(path, path.with_name("copy.jsonl"))
    refreshed = Ingestor(app.store).refresh()
    assert refreshed["status"] == "passed"
    assert len(records(app)) == 1
    evidence = app.evidence(records(app)[0]["id"])
    assert len(evidence["locations"]) == 2
    assert len({location["source_id"] for location in evidence["locations"]}) == 2


def test_source_rename_retains_identity_and_marks_disappearance(app: App, tmp_path: Path) -> None:
    path = source(app, tmp_path, message("one", "Task: retain source identity."))
    Ingestor(app.store).refresh()
    prior = checkpoint(app)
    renamed = path.with_name("moved.jsonl")
    path.rename(renamed)
    report = Ingestor(app.store).refresh()
    assert report["status"] == "passed"
    assert checkpoint(app)["id"] == prior["id"]
    assert checkpoint(app)["path"] == str(renamed)
    assert len(records(app)) == 1
    renamed.unlink()
    report = Ingestor(app.store).refresh()
    assert report["status"] == "partial"
    assert report["missing"] == 1
    assert app.evidence(records(app)[0]["id"])["status"] == "missing_or_superseded"


def test_source_returning_unchanged_recovers_current_evidence(app: App, tmp_path: Path) -> None:
    path = source(app, tmp_path, message("one", "Task: temporarily offline source."))
    Ingestor(app.store).refresh()
    # Configure a parent directory, then remove access to it without changing file metadata.
    original = path.parent
    moved = tmp_path / "temporarily-offline"
    original.rename(moved)
    assert Ingestor(app.store).refresh()["status"] == "partial"
    assert checkpoint(app)["status"] == "missing"
    moved.rename(original)
    recovered = Ingestor(app.store).refresh()
    assert recovered["status"] == "passed"
    assert checkpoint(app)["status"] == "ready", (
        "An unchanged returning file must stop being missing."
    )
    assert app.evidence(records(app)[0]["id"])["status"] == "current"


def test_interrupted_source_transaction_rolls_back_records_and_checkpoint(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = source(app, tmp_path, message("one", "Task: committed before interruption."))
    Ingestor(app.store).refresh()
    prior = checkpoint(app)
    with path.open("ab") as handle:
        handle.write(encoded(message("two", "Task: interrupted batch.")))
    original = Ingestor.record

    def interrupt(self: Ingestor, *args: Any) -> None:
        original(self, *args)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Ingestor, "record", interrupt)
        with pytest.raises(KeyboardInterrupt):
            Ingestor(app.store).refresh()
    assert checkpoint(app) == prior
    assert len(records(app)) == 1
    assert (
        app.store.rows("SELECT status FROM refresh_runs ORDER BY id DESC LIMIT 1")[0]["status"]
        == "interrupted"
    )
    assert Ingestor(app.store).refresh()["status"] == "passed"
    assert len(records(app)) == 2
    assert app.store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_hard_process_exit_preserves_committed_checkpoint(app: App, tmp_path: Path) -> None:
    path = source(app, tmp_path, message("one", "Task: committed before process exit."))
    Ingestor(app.store).refresh()
    prior = checkpoint(app)
    with path.open("ab") as handle:
        handle.write(encoded(message("two", "Task: uncommitted on process exit.")))
    script = """
import os, sys
from pathlib import Path
from rifja.ingest import Ingestor
from rifja.store import Store
original = Ingestor.record
def stop(self, *args):
    original(self, *args)
    os._exit(23)
Ingestor.record = stop
with Store(Path(sys.argv[1])) as store:
    Ingestor(store).refresh()
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run(
        [sys.executable, "-c", script, str(app.store.home)],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 23, result.stderr.decode()
    assert checkpoint(app) == prior
    assert len(records(app)) == 1
    assert Ingestor(app.store).refresh()["status"] == "passed"
    assert len(records(app)) == 2


def test_concurrent_refresh_is_bounded_and_does_not_duplicate_records(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source(app, tmp_path, message("one", "Task: concurrent refresh fixture."))
    entered, release = threading.Event(), threading.Event()
    outcomes: list[Any] = []
    original = Ingestor.read_jsonl

    def pause(self: Ingestor, *args: Any) -> None:
        entered.set()
        if not release.wait(timeout=8):
            raise TimeoutError("test synchronization failed")
        original(self, *args)

    def worker() -> None:
        try:
            with Store(app.store.home) as other:
                outcomes.append(Ingestor(other).refresh())
        except BaseException as exc:  # noqa: BLE001 - transport worker failures to the asserting thread
            outcomes.append(exc)

    monkeypatch.setattr(Ingestor, "read_jsonl", pause)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(timeout=3)
        started = time.monotonic()
        with pytest.raises(BusyError, match="busy"):
            Ingestor(app.store).refresh()
        assert time.monotonic() - started < 3.5
        # Reads remain possible while another connection has an uncommitted write.
        assert records(app) == []
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict), outcomes
    assert outcomes[0]["status"] == "passed"
    assert len(records(app)) == 1
    assert Ingestor(app.store).refresh()["unchanged"] == 1


def test_user_corrections_memory_and_forget_survive_rebuild_and_restore(
    app: App, tmp_path: Path
) -> None:
    source(app, tmp_path, message("one", "Task: inspect the retry failure."))
    Ingestor(app.store).refresh()
    item = app.items()["items"][0]
    correction = app.correct(
        item["id"], "blocked", "Retry waits for evidence.", "Explicit user correction"
    )
    memory = app.memory_add(
        "principle", "Verify every retry with evidence.", refs=[item["record_id"]]
    )
    Ingestor(app.store).refresh(rebuild=True)
    corrected = next(i for i in app.items()["items"] if i["id"] == item["id"])
    assert corrected["status"] == "blocked"
    assert corrected["text"] == "Retry waits for evidence."
    assert correction["id"] in corrected["resolution_refs"]
    assert app.principles()[0]["id"] == memory["id"]
    backup = tmp_path / "before-forget.sqlite3"
    app.store.backup(backup)
    restored_home = tmp_path / "restored"
    restore(backup, restored_home)
    with Store(restored_home) as restored:
        restored_app = App(restored)
        assert restored_app.items()["items"][0]["status"] == "blocked"
        assert restored_app.principles()[0]["id"] == memory["id"]
    sid = records(app)[0]["session_id"]
    assert app.forget(sid, confirm=True)["removed_records"] == 1
    assert records(app) == []
    assert app.search("retry")["matches"] == []
    Ingestor(app.store).refresh(rebuild=True)
    assert records(app) == []
    assert app.principles()[0]["evidence_status"] == ["removed_or_unknown"]
    assert app.store.rows("SELECT id FROM corrections")[0]["id"] == correction["id"]
    forgotten_backup = tmp_path / "after-forget.sqlite3"
    app.store.backup(forgotten_backup)
    forgotten_home = tmp_path / "restored-forgotten"
    restore(forgotten_backup, forgotten_home)
    with Store(forgotten_home) as restored:
        assert Ingestor(restored).refresh(rebuild=True)["forgotten_records"] == 1
        assert restored.rows("SELECT * FROM records") == []
        assert len(App(restored).principles()) == 1


def create_v1(home: Path, *, fail_migration: bool = False) -> None:
    home.mkdir()
    with sqlite3.connect(home / "state.sqlite3") as db:
        db.executescript(SCHEMA_V1)
        db.execute("INSERT INTO config VALUES('sentinel','\"preserve-me\"')")
        if fail_migration:
            db.execute("CREATE TABLE audit_log(unexpected_column TEXT)")


def test_v1_migration_preserves_data_and_recoverable_pre_migration_backup(tmp_path: Path) -> None:
    home = tmp_path / "v1"
    create_v1(home)
    with Store(home) as migrated:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.config("sentinel") == "preserve-me"
        assert migrated.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    backup = home / "before-migration-v1.sqlite3"
    assert backup.is_file()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT value FROM config WHERE key='sentinel'").fetchone()[0]
            == '"preserve-me"'
        )
    assert backup.stat().st_mode & 0o077 == 0


def test_failed_migration_rolls_back_and_preserves_original_version(tmp_path: Path) -> None:
    home = tmp_path / "failing-v1"
    create_v1(home, fail_migration=True)
    with pytest.raises(sqlite3.Error):
        Store(home)
    with sqlite3.connect(home / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT value FROM config WHERE key='sentinel'").fetchone()[0]
            == '"preserve-me"'
        )
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert (home / "before-migration-v1.sqlite3").is_file()


def test_v2_upgrade_backup_restore_and_failed_index_migration(tmp_path: Path) -> None:
    home = tmp_path / "v2"
    create_v1(home)
    with sqlite3.connect(home / "state.sqlite3") as db:
        db.execute(MIGRATION_V2)
        db.execute("PRAGMA user_version=2")
    with Store(home) as migrated:
        assert migrated.config("sentinel") == "preserve-me"
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    backup = home / "before-migration-v2.sqlite3"
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    restored_home = tmp_path / "restored-v2"
    restore(backup, restored_home)
    with Store(restored_home) as restored:
        assert restored.config("sentinel") == "preserve-me"
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    failing = tmp_path / "failing-v2"
    create_v1(failing)
    with sqlite3.connect(failing / "state.sqlite3") as db:
        db.execute(MIGRATION_V2)
        db.execute("PRAGMA user_version=2")
        db.execute("CREATE TABLE records_daily(unexpected_column TEXT)")
    with pytest.raises(sqlite3.Error):
        Store(failing)
    with sqlite3.connect(failing / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            db.execute("SELECT value FROM config WHERE key='sentinel'").fetchone()[0]
            == '"preserve-me"'
        )
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert (failing / "before-migration-v2.sqlite3").is_file()


def test_newer_schema_is_refused_without_mutating_database(tmp_path: Path) -> None:
    home = tmp_path / "newer"
    home.mkdir()
    database = home / "state.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        db.execute("CREATE TABLE future_data(value TEXT)")
        db.execute("INSERT INTO future_data VALUES('preserve future schema')")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="newer_schema"):
        Store(home)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not (home / "before-migration-v1.sqlite3").exists()


def test_restore_rejects_bad_backup_and_preserves_existing_state(app: App, tmp_path: Path) -> None:
    app.memory_add("fact", "Preserve the current durable state.")
    invalid = tmp_path / "invalid-backup.sqlite3"
    invalid.write_bytes(b"not a SQLite database")
    before = app.store.rows("SELECT * FROM memory")
    with pytest.raises((ValueError, sqlite3.Error)):
        restore(invalid, tmp_path / "bad-destination")
    assert not (tmp_path / "bad-destination").exists()
    valid = tmp_path / "valid-backup.sqlite3"
    app.store.backup(valid)
    with pytest.raises(ValueError, match="empty_destination"):
        restore(valid, app.store.home)
    assert app.store.rows("SELECT * FROM memory") == before


def test_restore_rejects_foreign_key_corruption(app: App, tmp_path: Path) -> None:
    backup = tmp_path / "inconsistent-backup.sqlite3"
    app.store.backup(backup)
    with sqlite3.connect(backup) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO sessions VALUES('orphan-session','claude','synthetic')")
        db.execute("INSERT INTO occurrences VALUES('missing-record',999,'line:1')")
        assert db.execute("PRAGMA foreign_key_check").fetchall()
    destination = tmp_path / "should-not-restore"
    with pytest.raises((ValueError, sqlite3.Error)):
        restore(backup, destination)
    assert not destination.exists()


def test_event_cwd_overrides_session_and_resume_refreshes_git(app: App, tmp_path: Path) -> None:
    first = repository(tmp_path / "project-a")
    second = repository(tmp_path / "project-b")
    aid = app.register(first)["project"]["id"]
    bid = app.register(second)["project"]["id"]
    data = tmp_path / "codex.jsonl"
    old_head = run_git(first, "rev-parse", "HEAD").decode().strip()
    data.write_bytes(
        encoded(
            {
                "type": "session_meta",
                "timestamp": "2025-01-02T12:00:00Z",
                "payload": {"id": "cwd-session", "cwd": str(first)},
            },
            {
                "type": "response_item",
                "timestamp": "2025-01-02T12:00:01Z",
                "payload": {
                    "id": "action",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Task: add an observed retry."}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2025-01-02T12:00:02Z",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "id": "historical-test",
                        "type": "command_execution",
                        "cwd": str(second),
                        "exit_code": 0,
                        "revision": old_head,
                        "aggregated_output": "Ran 14 tests. OK",
                    },
                },
            },
        )
    )
    app.source_add("codex", data)
    assert Ingestor(app.store).refresh()["status"] == "passed"
    event = next(r for r in records(app) if r["native_id"] == "completed:historical-test")
    assert event["project_id"] == bid
    action = next(r for r in records(app) if r["native_id"] == "action")
    assert action["project_id"] == aid
    run_git(first, "commit", "--allow-empty", "-m", "New current revision")
    (first / "source.txt").write_text("dirty current checkout\n")
    resumed = app.resume(aid)
    observation = resumed["worktrees"][0]["observation"]
    assert observation["head"] != old_head
    assert observation["status"]
    assert any("Historical transcript tests" in text for text in resumed["uncertainties"])
    assert app.evidence(event["id"])["metadata"]["revision"] == old_head
    assert app.evidence(event["id"])["category"] == "recorded_tool_result"


def test_missing_event_cwd_is_not_confidently_attributed_to_old_registered_path(
    app: App, tmp_path: Path
) -> None:
    path = repository(tmp_path / "old-project")
    pid = app.register(path)["project"]["id"]
    moved = tmp_path / "moved-project"
    path.rename(moved)
    source(app, tmp_path, message("one", "Task: unknown checkout after move.", cwd=path))
    Ingestor(app.store).refresh()
    row = records(app)[0]
    assert row["project_id"] is None, (
        "A missing cwd must remain unresolved rather than match a stale registration."
    )
    assert app.resume(pid)["worktrees"][0]["observation"]["available"] is False


@pytest.mark.parametrize("bad_record", ["unknown", "oversized", "deep", "invalid"])
def test_invalid_records_are_bounded_and_do_not_poison_valid_neighbors(
    app: App, tmp_path: Path, bad_record: str
) -> None:
    path = source(app, tmp_path, message("before", "Task: preserve preceding valid item."))
    if bad_record == "unknown":
        raw = encoded({"type": "never-seen", "sessionId": "synthetic-session"})
    elif bad_record == "oversized":
        raw = b'{"padding":"' + b"x" * MAX_RECORD_BYTES + b'"}\n'
    elif bad_record == "deep":
        raw = b'{"nested":' + b"[" * (MAX_DEPTH + 2) + b"0" + b"]" * (MAX_DEPTH + 2) + b"}\n"
    else:
        raw = b'{"broken":\n'
    with path.open("ab") as handle:
        handle.write(raw)
        handle.write(encoded(message("after", "Next: preserve following valid item.")))
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    result = Ingestor(app.store).refresh()
    assert result["status"] == "partial"
    assert {r["text"] for r in records(app)} == {
        "Task: preserve preceding valid item.",
        "Next: preserve following valid item.",
    }
    diagnostics = json.loads(checkpoint(app)["diagnostics"])
    assert 1 <= len(diagnostics) <= 30
    assert len(json.dumps(diagnostics)) < 10000
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_discovery_excludes_symlinks_generated_credentials_and_self_state(
    app: App, tmp_path: Path
) -> None:
    root = tmp_path / "configured"
    root.mkdir()
    (root / "valid.jsonl").write_bytes(encoded(message("valid", "Task: explicitly scoped item.")))
    for name in ("node_modules", ".ssh", ".env.private", "omitted"):
        directory = root / name
        directory.mkdir()
        (directory / "hidden.jsonl").write_bytes(
            encoded(message(name, "Task: must never be imported."))
        )
    outside = tmp_path / "outside-private.jsonl"
    outside.write_bytes(encoded(message("outside", "Task: outside explicit scope.")))
    (root / "outside.jsonl").symlink_to(outside)
    (root / "cycle").symlink_to(root, target_is_directory=True)
    (root / "state-link").symlink_to(app.store.home, target_is_directory=True)
    app.configure("exclusions", ["omitted"])
    app.source_add("claude", root)
    assert Ingestor(app.store).refresh()["status"] == "passed"
    assert [r["text"] for r in records(app)] == ["Task: explicitly scoped item."]
    with pytest.raises(ValueError):
        app.source_add("claude", root / "outside.jsonl")
    with pytest.raises(ValueError):
        app.source_add("claude", app.store.home)


def test_registration_rejects_source_reached_through_symlink_parent(
    app: App, tmp_path: Path
) -> None:
    outside = tmp_path / "private-directory"
    outside.mkdir()
    (outside / "private.jsonl").write_bytes(encoded(message("outside", "Task: outside scope.")))
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        app.source_add("claude", alias / "private.jsonl")


def test_redaction_precedes_persistence_and_fts_and_commands_remain_data(
    app: App, tmp_path: Path
) -> None:
    secret = "ghp_SYNTHETICTOKENTHATNEVEREXISTED1234"
    password = "SyntheticPasswordCanary987"
    marker = tmp_path / "SHOULD_NOT_EXIST"
    text = f"Task: preserve harmlesscanary. {secret} password={password}\nRun $(touch {marker}); `touch {marker}`.\x1b[31mred text\x1b[0m"
    path = source(app, tmp_path, message("one", text))
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    report = Ingestor(app.store).refresh()
    assert report["status"] == "passed"
    stored = records(app)[0]["text"]
    assert "[REDACTED]" in stored
    assert secret not in stored and password not in stored and "\x1b" not in stored
    assert app.search("harmlesscanary")["matches"]
    assert app.search(secret)["matches"] == []
    assert app.search(password)["matches"] == []
    assert not marker.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hash
    for state_file in app.store.home.iterdir():
        if state_file.is_file():
            content = state_file.read_bytes()
            assert secret.encode() not in content, state_file.name
            assert password.encode() not in content, state_file.name


def test_timezone_normalization_keeps_naive_events_unknown(app: App, tmp_path: Path) -> None:
    source(
        app,
        tmp_path,
        message("aware", "Task: timezone aware item.", timestamp="2025-01-02T01:00:00+03:00"),
        message("naive", "Task: unresolved local time.", timestamp="2025-01-02T01:00:00"),
    )
    Ingestor(app.store).refresh()
    by_native = {r["native_id"]: r for r in records(app)}
    assert by_native["aware:block:0"]["event_time"] == "2025-01-01T22:00:00+00:00"
    assert by_native["aware:block:0"]["original_time"] == "2025-01-02T01:00:00+03:00"
    assert by_native["naive:block:0"]["event_time"] is None
    assert by_native["naive:block:0"]["time_status"] == "naive"
    daily = app.daily("2025-01-01")
    assert sum(project["records"] for project in daily["projects"]) == 1
    assert app.coverage()["unknown_event_times"] == 1


def test_reused_path_with_different_git_identity_is_not_attributed_to_old_project(
    app: App, tmp_path: Path
) -> None:
    path = repository(tmp_path / "same-location")
    app.register(path)
    path.rename(tmp_path / "original-project-moved")
    repository(path)
    source(app, tmp_path, message("one", "Task: another repository at an old path.", cwd=path))
    Ingestor(app.store).refresh()
    assert records(app)[0]["project_id"] is None, (
        "A replacement repository must not inherit the original project's identity."
    )


def test_many_oversized_records_keep_diagnostics_within_the_bound(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = source(app, tmp_path, message("one", "Task: valid neighbor."))
    monkeypatch.setattr("rifja.ingest.MAX_RECORD_BYTES", 512)
    with path.open("ab") as handle:
        handle.write((b'{"padding":"' + b"x" * 1024 + b'"}\n') * 80)
    report = Ingestor(app.store).refresh()
    assert report["status"] == "partial"
    assert len(records(app)) == 1
    diagnostics = json.loads(checkpoint(app)["diagnostics"])
    assert len(diagnostics) <= 30, "Oversized-record skips must obey the same diagnostic bound."


def test_hermes_wal_refresh_observes_committed_rows_without_mutating_producer(
    app: App, tmp_path: Path
) -> None:
    path = tmp_path / "producer.sqlite3"
    with sqlite3.connect(path) as producer:
        producer.execute("PRAGMA journal_mode=WAL")
        producer.execute("PRAGMA wal_autocheckpoint=0")
        producer.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,started_at REAL,cwd TEXT)")
        producer.execute(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL)"
        )
        producer.execute("INSERT INTO sessions VALUES('hermes-session',1735819200,NULL)")
        producer.execute(
            "INSERT INTO messages VALUES(1,'hermes-session','user','Task: first durable row.',1735819200)"
        )
        producer.commit()
        app.source_add("hermes", path)
        tracked = [path, Path(str(path) + "-wal")]
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
        assert Ingestor(app.store).refresh()["status"] == "passed"
        assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked} == before
        assert len(records(app)) == 1
        producer.execute(
            "INSERT INTO messages VALUES(2,'hermes-session','user','Next: second WAL row.',1735819201)"
        )
        # Uncommitted data must not cross the producer's transaction boundary.
        assert Ingestor(app.store).refresh()["status"] == "passed"
        assert len(records(app)) == 1
        producer.commit()
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
        assert Ingestor(app.store).refresh()["status"] == "passed"
        assert len(records(app)) == 2
        assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked} == before
        producer.execute("UPDATE messages SET content='Task: edited durable row.' WHERE id=1")
        producer.commit()
        assert Ingestor(app.store).refresh()["status"] == "passed"
        prior = next(r for r in records(app) if r["text"] == "Task: first durable row.")
        assert app.evidence(prior["id"])["status"] == "missing_or_superseded"
        assert len(records(app)) == 3


def test_exclusion_removes_existing_index_and_prevents_copy_reimport(
    app: App, tmp_path: Path
) -> None:
    path = source(app, tmp_path, message("one", "Task: previouslycollectedcanary."))
    Ingestor(app.store).refresh()
    sid = records(app)[0]["session_id"]
    app.configure("exclusions", [path.name])
    assert app.search("previouslycollectedcanary")["matches"] == []
    assert app.store.rows("SELECT * FROM sessions WHERE id=?", (sid,)) == []
    shutil.copy2(path, path.with_name("copy.jsonl"))
    Ingestor(app.store).refresh(rebuild=True)
    assert records(app) == []


def test_ambiguous_native_cancellation_does_not_cancel_two_sessions(
    app: App, tmp_path: Path
) -> None:
    path = repository(tmp_path / "shared-context")
    app.register(path)
    source(
        app,
        tmp_path,
        message("task-1", "Task: first independent task.", session="session-one", cwd=path),
        message("task-1", "Task: second independent task.", session="session-two", cwd=path),
        message(
            "cancel-1",
            "CANCEL: [task-1:block:0] No reliable target supplied.",
            session="session-three",
            cwd=path,
            timestamp="2025-01-02T12:01:00Z",
        ),
    )
    Ingestor(app.store).refresh()
    tasks = [i for i in app.items()["items"] if i["kind"] == "task"]
    assert len(tasks) == 2
    assert all(task["status"] == "active" for task in tasks), (
        "An ambiguous native ID must not authorize multiple state changes."
    )


def test_earlier_cancellation_does_not_resolve_a_future_task(app: App, tmp_path: Path) -> None:
    path = repository(tmp_path / "chronological-context")
    app.register(path)
    source(
        app,
        tmp_path,
        message(
            "cancel-1",
            "CANCEL: [future-1:block:0] Earlier unresolved reference.",
            cwd=path,
            timestamp="2025-01-02T11:00:00Z",
        ),
        message(
            "future-1",
            "Task: later newly created task.",
            cwd=path,
            timestamp="2025-01-02T12:00:00Z",
        ),
    )
    Ingestor(app.store).refresh()
    task = next(i for i in app.items()["items"] if i["kind"] == "task")
    assert task["status"] == "active", (
        "Only later authoritative corrections can resolve an existing task."
    )
