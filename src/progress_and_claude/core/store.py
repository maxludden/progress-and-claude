"""SQLite (WAL) persistence shared by the MCP server, CLI and TUI processes.

Every write bumps a global ``revision`` counter inside the same transaction, so
any process can detect changes with a single cheap read (:meth:`Store.revision`)
instead of needing a daemon or sockets.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from progress_and_claude.core.models import BoardInfo, Status, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', 0);
CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    board TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    id TEXT NOT NULL, parent_id TEXT, position INTEGER NOT NULL,
    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, completed REAL NOT NULL DEFAULT 0, total REAL,
    weight REAL NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    started_at REAL, finished_at REAL,
    PRIMARY KEY (board, id)
);
CREATE TABLE IF NOT EXISTS events (board TEXT NOT NULL, task_id TEXT NOT NULL, ts REAL NOT NULL, progress REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_lookup ON events (board, task_id, ts);
CREATE TABLE IF NOT EXISTS name_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL);
"""

_TASK_COLUMNS = (
    "board, id, parent_id, position, name, description, status, completed, total, weight, "
    "note, created_at, updated_at, started_at, finished_at"
)
_MAX_EVENTS_PER_TASK = 60


class Tx:
    """A short-lived handle on one SQLite connection with typed helpers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Wrap a connection.

        Args:
            conn: An open connection using ``sqlite3.Row`` rows.
        """
        self._conn = conn

    def revision(self) -> int:
        """Return the global write counter."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
        return int(row["value"])

    def board_title(self, board: str) -> str | None:
        """Return a board's title, or ``None`` if it does not exist."""
        row = self._conn.execute("SELECT title FROM boards WHERE id = ?", (board,)).fetchone()
        return None if row is None else str(row["title"])

    def upsert_board(self, board: str, title: str, now: float) -> None:
        """Create a board or update its title and timestamp."""
        self._conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at",
            (board, title, now, now),
        )

    def touch_board(self, board: str, now: float) -> None:
        """Record that a board changed."""
        self._conn.execute("UPDATE boards SET updated_at = ? WHERE id = ?", (now, board))

    def list_boards(self) -> list[BoardInfo]:
        """Return every board, most recently updated first (without progress figures)."""
        rows = self._conn.execute(
            "SELECT id, title, created_at, updated_at FROM boards ORDER BY updated_at DESC"
        ).fetchall()
        return [
            BoardInfo(
                id=r["id"], title=r["title"], created_at=r["created_at"], updated_at=r["updated_at"]
            )
            for r in rows
        ]

    def delete_board(self, board: str) -> bool:
        """Delete a board with its tasks and events; return whether it existed."""
        self._conn.execute("DELETE FROM tasks WHERE board = ?", (board,))
        self._conn.execute("DELETE FROM events WHERE board = ?", (board,))
        return self._conn.execute("DELETE FROM boards WHERE id = ?", (board,)).rowcount > 0

    def clear_tasks(self, board: str) -> None:
        """Remove all tasks and events of a board but keep the board row."""
        self._conn.execute("DELETE FROM tasks WHERE board = ?", (board,))
        self._conn.execute("DELETE FROM events WHERE board = ?", (board,))

    def tasks(self, board: str) -> list[Task]:
        """Return every task of a board in insertion order."""
        rows = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE board = ? ORDER BY rowid", (board,)
        ).fetchall()
        return [_task_from_row(r) for r in rows]

    def insert_task(self, task: Task) -> None:
        """Insert a new task."""
        self._conn.execute(
            f"INSERT INTO tasks ({_TASK_COLUMNS}) VALUES ({', '.join('?' * 15)})",
            _task_values(task),
        )

    def save_task(self, task: Task) -> None:
        """Persist the mutable fields of an existing task."""
        self._conn.execute(
            "UPDATE tasks SET name = ?, description = ?, status = ?, completed = ?, total = ?, "
            "weight = ?, note = ?, updated_at = ?, started_at = ?, finished_at = ? "
            "WHERE board = ? AND id = ?",
            (
                task.name, task.description, task.status.value, task.completed, task.total,
                task.weight, task.note, task.updated_at, task.started_at, task.finished_at,
                task.board, task.id,
            ),
        )  # fmt: skip

    def add_events(self, board: str, rows: list[tuple[str, float, float]]) -> None:
        """Append ``(task_id, ts, progress)`` samples and prune old ones."""
        for task_id, ts, progress in rows:
            self._conn.execute(
                "INSERT INTO events (board, task_id, ts, progress) VALUES (?, ?, ?, ?)",
                (board, task_id, ts, progress),
            )
            self._conn.execute(
                "DELETE FROM events WHERE board = ? AND task_id = ? AND rowid NOT IN ("
                "SELECT rowid FROM events WHERE board = ? AND task_id = ? ORDER BY ts DESC LIMIT ?)",
                (board, task_id, board, task_id, _MAX_EVENTS_PER_TASK),
            )

    def samples(self, board: str) -> dict[str, list[tuple[float, float]]]:
        """Return ``(ts, progress)`` samples per task id (``""`` is the whole board)."""
        out: dict[str, list[tuple[float, float]]] = {}
        for r in self._conn.execute(
            "SELECT task_id, ts, progress FROM events WHERE board = ? ORDER BY ts", (board,)
        ):
            out.setdefault(r["task_id"], []).append((r["ts"], r["progress"]))
        return out

    def cache_get(self, key: str) -> str | None:
        """Return a cached value, or ``None``."""
        row = self._conn.execute("SELECT value FROM name_cache WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def cache_put(self, key: str, value: str, now: float) -> None:
        """Store a cached value."""
        self._conn.execute(
            "INSERT OR REPLACE INTO name_cache (key, value, created_at) VALUES (?, ?, ?)",
            (key, value, now),
        )

    def bump(self) -> None:
        """Increment the global revision counter."""
        self._conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'revision'")


def _task_values(t: Task) -> tuple[object, ...]:
    """Flatten a task into column order for INSERT."""
    return (
        t.board, t.id, t.parent_id, t.position, t.name, t.description, t.status.value,
        t.completed, t.total, t.weight, t.note, t.created_at, t.updated_at, t.started_at,
        t.finished_at,
    )  # fmt: skip


def _task_from_row(r: sqlite3.Row) -> Task:
    """Convert a database row into a :class:`Task`."""
    return Task(
        board=r["board"], id=r["id"], parent_id=r["parent_id"], position=r["position"],
        name=r["name"], description=r["description"], status=Status(r["status"]),
        completed=r["completed"], total=r["total"], weight=r["weight"], note=r["note"],
        created_at=r["created_at"], updated_at=r["updated_at"], started_at=r["started_at"],
        finished_at=r["finished_at"],
    )  # fmt: skip


class Store:
    """Opens short-lived connections to the shared database file."""

    def __init__(self, path: Path) -> None:
        """Create the database file and schema if needed.

        Args:
            path: Location of the SQLite file.
        """
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection in autocommit mode with row access by name."""
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def read(self) -> Iterator[Tx]:
        """Yield a read-only handle with a consistent snapshot of the database."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield Tx(conn)
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        """Yield a write handle; commit (with a revision bump) or roll back on error."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            tx = Tx(conn)
            # Bump first so snapshots built inside the transaction report the new revision;
            # the bump commits (or rolls back) atomically with the rest of the write.
            tx.bump()
            yield tx
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def revision(self) -> int:
        """Return the global write counter (cheap change detection for watchers)."""
        with self.read() as tx:
            return tx.revision()
