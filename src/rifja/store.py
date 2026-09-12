"""Transactional SQLite state with durable memory separate from derived indexes."""

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from .privacy import clean_text, private_dir
from .timeutil import now

SCHEMA_VERSION = 4
MIGRATION_V2 = "CREATE TABLE audit_log(id INTEGER PRIMARY KEY,action TEXT NOT NULL,target TEXT NOT NULL,at TEXT NOT NULL,details TEXT NOT NULL)"
MIGRATION_V3 = "CREATE INDEX records_daily ON records(event_time,project_id,provider,actor,session_id,worktree_id)"
MIGRATION_V4 = "CREATE TABLE activity(id INTEGER PRIMARY KEY,at TEXT NOT NULL,surface TEXT NOT NULL,tool TEXT NOT NULL,summary TEXT NOT NULL,status TEXT NOT NULL,duration_ms INTEGER NOT NULL)"

SCHEMA_V1 = """
CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,common_identity TEXT UNIQUE,common_dir TEXT,created_at TEXT NOT NULL);
CREATE TABLE worktrees(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),identity TEXT UNIQUE,path TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE observations(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL,worktree_id TEXT NOT NULL,observed_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX observations_worktree ON observations(worktree_id,id DESC);
CREATE TABLE sources(id TEXT PRIMARY KEY,provider TEXT NOT NULL,path TEXT NOT NULL,identity TEXT,status TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0,signature TEXT,offset INTEGER NOT NULL DEFAULT 0,line INTEGER NOT NULL DEFAULT 0,prefix_hash TEXT,context TEXT NOT NULL DEFAULT '{}',last_refresh TEXT,diagnostics TEXT NOT NULL DEFAULT '[]',UNIQUE(provider,path));
CREATE TABLE generations(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),number INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source_id,number));
CREATE TABLE sessions(id TEXT PRIMARY KEY,provider TEXT NOT NULL,native_id TEXT NOT NULL,UNIQUE(provider,native_id));
CREATE TABLE records(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),native_id TEXT NOT NULL,provider TEXT NOT NULL,actor TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,event_time TEXT,original_time TEXT,time_status TEXT NOT NULL,cwd TEXT,metadata TEXT NOT NULL,project_id TEXT,worktree_id TEXT,association_reason TEXT,imported_at TEXT NOT NULL);
CREATE INDEX records_project_time ON records(project_id,event_time);
CREATE INDEX records_session ON records(session_id,event_time);
CREATE INDEX records_native ON records(native_id);
CREATE TABLE occurrences(record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,generation_id INTEGER NOT NULL REFERENCES generations(id),locator TEXT NOT NULL,PRIMARY KEY(record_id,generation_id,locator));
CREATE INDEX occurrences_generation ON occurrences(generation_id);
CREATE TABLE items(id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,kind TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,category TEXT NOT NULL,target TEXT,rationale TEXT,priority TEXT,dependencies TEXT NOT NULL,method TEXT NOT NULL);
CREATE INDEX items_record ON items(record_id);
CREATE INDEX items_kind ON items(kind,status);
CREATE TABLE memory(id TEXT PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,origin TEXT NOT NULL,scope TEXT NOT NULL,refs TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,supersedes TEXT,exceptions TEXT,conflicts TEXT);
CREATE TABLE corrections(id TEXT PRIMARY KEY,target TEXT NOT NULL,text TEXT,status TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX corrections_target ON corrections(target,created_at);
CREATE TABLE associations(id TEXT PRIMARY KEY,target TEXT NOT NULL,project_id TEXT NOT NULL,worktree_id TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE forgotten(provider TEXT NOT NULL,session_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(provider,session_id));
CREATE TABLE refresh_runs(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,ended_at TEXT,status TEXT NOT NULL,stats TEXT NOT NULL);
CREATE VIRTUAL TABLE records_fts USING fts5(text,content='records',content_rowid='rowid');
CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN INSERT INTO records_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE TRIGGER records_ad AFTER DELETE ON records BEGIN INSERT INTO records_fts(records_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
CREATE TRIGGER records_au AFTER UPDATE OF text ON records BEGIN INSERT INTO records_fts(records_fts,rowid,text) VALUES('delete',old.rowid,old.text); INSERT INTO records_fts(rowid,text) VALUES(new.rowid,new.text); END;
PRAGMA user_version=1;
"""


class BusyError(Exception):
    pass


class Store:
    def __init__(self, home: Path):
        self.home = home.expanduser().absolute()
        private_dir(self.home)
        self.path = self.home / "state.sqlite3"
        if self.path.is_symlink():
            raise ValueError("database_must_not_be_symlink")
        self.db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"newer_schema: {version}; supported: {SCHEMA_VERSION}")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=2000")
            if version == 0:
                if self.db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]:
                    raise ValueError("unrecognized_database")
                self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V1 + "\nCOMMIT;")
                version = 1
            if version == 1:
                backup = self.home / "before-migration-v1.sqlite3"
                if not backup.exists():
                    self.backup(backup)
                with self.transaction():
                    self.db.execute(MIGRATION_V2)
                    self.db.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                backup = self.home / "before-migration-v2.sqlite3"
                if not backup.exists():
                    self.backup(backup)
                with self.transaction():
                    self.db.execute(MIGRATION_V3)
                    self.db.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                backup = self.home / "before-migration-v3.sqlite3"
                if not backup.exists():
                    self.backup(backup)
                with self.transaction():
                    # v4: append-only agent/operator activity record — the
                    # observability feed for the management dashboard.
                    self.db.execute(MIGRATION_V4)
                    self.db.execute("PRAGMA user_version=4")
            self.db.execute("PRAGMA journal_mode=WAL")
            # Bounded local cache and checkpoint batching reduce repeated page
            # churn during large refreshes. FULL commit synchronization and
            # per-source transactions remain unchanged; close includes cleanup.
            self.db.execute("PRAGMA cache_size=-16384")
            self.db.execute("PRAGMA wal_autocheckpoint=4096")
            os.chmod(self.path, 0o600)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, write: bool = True) -> Iterator[None]:
        if self.db.in_transaction:
            name = "nested_" + uuid4().hex
            self.db.execute(f"SAVEPOINT {name}")
            try:
                yield
                self.db.execute(f"RELEASE {name}")
            except BaseException as exc:
                self.db.execute(f"ROLLBACK TO {name}")
                self.db.execute(f"RELEASE {name}")
                if isinstance(exc, sqlite3.OperationalError) and getattr(
                    exc, "sqlite_errorcode", 0
                ) & 255 in (5, 6):
                    raise BusyError("state changed during observation; retry the command") from exc
                raise
            return
        try:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        except sqlite3.OperationalError as exc:
            raise BusyError("state busy; retry shortly") from exc
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException as exc:
            self.db.execute("ROLLBACK")
            if isinstance(exc, sqlite3.OperationalError) and getattr(
                exc, "sqlite_errorcode", 0
            ) & 255 in (5, 6):
                raise BusyError("state changed during observation; retry the command") from exc
            raise

    @contextmanager
    def writer_lock(self, timeout: float = 2) -> Iterator[None]:
        import fcntl

        path = self.home / "refresh.lock"
        if path.is_symlink():
            raise ValueError("lock_must_not_be_symlink")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BusyError("refresh busy; another writer holds the lock") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args)]

    def config(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_config(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def log_activity(
        self, surface: str, tool: str, summary: str, status: str, duration_ms: int
    ) -> None:
        """Append one agent/operator execution record; never blocks the caller.

        Observability must not reduce availability: a failure to log is
        swallowed (best-effort), and the insert runs in its own short
        transaction so read snapshots are untouched.
        """
        try:
            with self.transaction():
                self.db.execute(
                    "INSERT INTO activity(at,surface,tool,summary,status,duration_ms)"
                    " VALUES(?,?,?,?,?,?)",
                    (now(), surface, tool, clean_text(summary, 400), status, int(duration_ms)),
                )
        except sqlite3.Error:
            pass

    def audit(self, action: str, target: str, details: Any) -> None:
        self.db.execute(
            "INSERT INTO audit_log(action,target,at,details) VALUES(?,?,?,?)",
            (action, target, now(), json.dumps(details, ensure_ascii=False)),
        )

    def backup(self, destination: Path) -> None:
        destination = destination.expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise ValueError("backup_destination_exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with sqlite3.connect(destination) as target:
                self.db.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("backup_integrity_failed")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def restore(backup: Path, home: Path) -> dict[str, Any]:
    """Restore a validated snapshot into a new/empty state directory only."""
    import shutil
    import tempfile

    if not backup.is_file() or backup.is_symlink():
        raise ValueError("invalid_backup_path")
    if home.exists() and any(home.iterdir()):
        raise ValueError("restore_requires_empty_destination; existing state is preserved")
    with sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.execute("PRAGMA query_only=ON")
        version = source.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2, 3, 4):
            raise ValueError("unsupported_backup_schema")
        validate_schema(source, version)
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("backup_integrity_failed")
        required = {"config", "records", "memory", "corrections", "forgotten"}
        actual = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= actual:
            raise ValueError("invalid_backup_structure")
        home.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".restore-", dir=home.parent))
        try:
            with sqlite3.connect(temp / "state.sqlite3") as target:
                source.backup(target)
            with Store(temp) as restored:
                if restored.db.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("backup_foreign_key_integrity_failed")
            if home.exists():
                home.rmdir()
            os.replace(temp, home)
        except BaseException:
            shutil.rmtree(temp)
            raise
    return {"restored": True, "schema_version": SCHEMA_VERSION, "destination": str(home)}


def validate_schema(connection: sqlite3.Connection, version: int) -> None:
    """Reject substituted trigger/view programs and unexpected schema objects."""
    with sqlite3.connect(":memory:") as expected:
        expected.executescript(SCHEMA_V1)
        if version >= 2:
            expected.execute(MIGRATION_V2)
        if version >= 3:
            expected.execute(MIGRATION_V3)
        if version >= 4:
            expected.execute(MIGRATION_V4)
        sql = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        actual = [tuple(row) for row in connection.execute(sql)]
        canonical = [tuple(row) for row in expected.execute(sql)]
        if actual != canonical:
            raise ValueError("backup_schema_does_not_match_supported_definition")
