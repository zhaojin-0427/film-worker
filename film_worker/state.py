"""SQLite state for leased attempts, durable completion and status."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from threading import Lock


# Closing the last WAL connection can take an exclusive lock. Keep the panel,
# scheduler and lease threads from opening or closing connections concurrently.
DATABASE_LOCK = Lock()


class InstanceBusy(BlockingIOError):
    """Another process is already holding this lock."""


class StateBusy(Exception):
    """SQLite could not obtain a database lock within its busy timeout."""


@contextmanager
def instance_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "worker.lock"
    with path.open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if isinstance(exc,BlockingIOError) or (os.name=="nt" and exc.errno in {13,36}):
                raise InstanceBusy("该数据目录已经有执行端在运行") from exc
            raise
        yield


class State:
    def __init__(self, root):
        self.path = root / "worker.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                work TEXT NOT NULL, number INTEGER NOT NULL, assignment TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', pending TEXT, receipt TEXT, message TEXT,
                updated REAL NOT NULL DEFAULT (unixepoch()), PRIMARY KEY (work, number)
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                work TEXT NOT NULL, number INTEGER NOT NULL, sequence INTEGER NOT NULL,
                event TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(work, number, sequence)
            )""")

    @contextmanager
    def connection(self):
        try:
            with DATABASE_LOCK:
                db = sqlite3.connect(self.path)
                db.row_factory = sqlite3.Row
                try:
                    with db:
                        yield db
                finally:
                    db.close()
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise StateBusy("本机数据库暂忙，正在重试") from exc
            raise

    def setting(self, name):
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else None

    def set(self, name, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, json.dumps(value)))

    def start(self, assignment):
        work, number = identity(assignment)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO attempts(work, number, assignment) VALUES (?, ?, ?)",
                       (work, number, json.dumps(assignment)))
            row = db.execute("SELECT status FROM attempts WHERE work=? AND number=?", (work, number)).fetchone()
            return row["status"] == "active"

    def mark(self, assignment, *, status="active", pending=None, receipt=None, message=None):
        work, number = identity(assignment)
        with self.connection() as db:
            db.execute("""UPDATE attempts SET status=?, pending=COALESCE(?, pending),
                receipt=COALESCE(?, receipt), message=?, updated=unixepoch() WHERE work=? AND number=?""",
                       (status, json.dumps(pending) if pending is not None else None,
                        json.dumps(receipt) if receipt is not None else None, message, work, number))

    def rows(self, only_open=False, limit=-1, *, states=()):
        with self.connection() as db:
            states = ("active",) if only_open else states
            condition = f" WHERE status IN ({','.join('?' for _ in states)})" if states else ""
            query = "SELECT * FROM attempts" + condition + " ORDER BY updated DESC LIMIT ?"
            return [dict(row) for row in db.execute(query, (*states, limit))]

    def event(self, assignment, status, *, level="info", message=None):
        work, number = identity(assignment)
        with self.connection() as db:
            last = db.execute("""SELECT sequence, event FROM events WHERE work=? AND number=?
                                 ORDER BY sequence DESC LIMIT 1""", (work, number)).fetchone()
            stage = status["stage"]
            if last and json.loads(last["event"])["stage"] == stage:
                return
            sequence = last["sequence"]+1 if last else 1
            data = dict(status, event_id=str(uuid.uuid4()), sequence=sequence, level=level,
                        occurred_at=datetime.now(timezone.utc).isoformat(), message=message or stage)
            db.execute("INSERT INTO events(work, number, sequence, event) VALUES (?, ?, ?, ?)",
                       (work, number, sequence, json.dumps(data)))

    def queued_events(self, assignment):
        with self.connection() as db:
            # Retry the exact event key and body after a response is lost, even if later
            # events were appended before the acknowledgement arrived.
            return [json.loads(row[0]) for row in db.execute("""SELECT event FROM events WHERE work=? AND number=?
                AND accepted=0 ORDER BY sequence LIMIT 1""", identity(assignment))]

    def accept_events(self, assignment, sequence):
        with self.connection() as db:
            db.execute("UPDATE events SET accepted=1 WHERE work=? AND number=? AND sequence<=?",
                       (*identity(assignment), sequence))


def identity(assignment):
    return assignment["work_id"], assignment["lease"]["attempt"]


@contextmanager
def slot(root, name, count, stop=None):
    import time
    while True:
        if stop is not None and stop.is_set():
            from .protocol import Interrupted
            raise Interrupted
        for i in range(count):
            lock = instance_lock(root / f"{name}-{i}")
            try:
                lock.__enter__()
            except (BlockingIOError, PermissionError):
                continue
            except OSError as exc:
                if os.name != "nt" or exc.errno not in (13, 36):
                    raise
                continue
            try:
                yield
            finally:
                lock.__exit__(None, None, None)
            return
        time.sleep(0.3)
