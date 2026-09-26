"""A `Vault` kept in an SQLite file, so mappings outlive the process."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
from types import TracebackType

from ..placeholders import format_placeholder, validate_entity_type

_SCHEMA_VERSION = 1

# Values and session names are stored as UTF-8 BLOBs written with
# "surrogatepass": text can hold lone surrogates, which sqlite3 would refuse
# to encode as TEXT. Placeholders and entity types are ASCII.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vault_meta ("
    " key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vault_sessions ("
    " session BLOB PRIMARY KEY, generation INTEGER NOT NULL,"
    " last_used REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vault_values ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, session BLOB NOT NULL,"
    " placeholder TEXT NOT NULL, value BLOB NOT NULL,"
    " UNIQUE (session, placeholder), UNIQUE (session, value))",
    "CREATE TABLE IF NOT EXISTS vault_counters ("
    " session BLOB NOT NULL, entity_type TEXT NOT NULL, number INTEGER NOT NULL,"
    " PRIMARY KEY (session, entity_type))",
    "CREATE TABLE IF NOT EXISTS vault_remembered ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, session BLOB NOT NULL,"
    " value BLOB NOT NULL, entity_type TEXT NOT NULL, placeholder TEXT NOT NULL,"
    " UNIQUE (session, value))",
    "CREATE TABLE IF NOT EXISTS vault_merged ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, session BLOB NOT NULL,"
    " placeholder TEXT NOT NULL, UNIQUE (session, placeholder))",
    "CREATE INDEX IF NOT EXISTS vault_values_by_session ON vault_values (session, id)",
    "CREATE INDEX IF NOT EXISTS vault_remembered_by_session"
    " ON vault_remembered (session, id)",
    "CREATE INDEX IF NOT EXISTS vault_merged_by_session ON vault_merged (session, id)",
)

_SESSION_TABLES = ("vault_values", "vault_counters", "vault_remembered", "vault_merged")

# A generation no session has: the cache must be reloaded from scratch.
_STALE = -1

# Connections inherited across fork(). SQLite forbids using them in the child,
# and closing them there could drop file locks the process holds through its
# own connection, so they are kept here, untouched, for the life of the process.
_INHERITED: list[sqlite3.Connection] = []


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogatepass")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogatepass")


class SQLiteVault:
    """Keeps value <-> placeholder mappings in an SQLite database file.

    Mappings survive the process, so a new `Shield` on the same file and
    session picks up the conversation's placeholders: the same value keeps
    its placeholder, restoring works across runs, and the leak check still
    knows the spellings merged by ``normalize=True`` and the matches hidden
    inside a merged placeholder. Registered entities and custom patterns are
    `Shield` settings, not vault contents: set them up on every `Shield`.
    Numbering is per entity type, as in `MemoryVault`.

    One file holds any number of sessions, one per conversation; each session
    has its own values and numbering, and `clear` only empties its own. Several
    processes and threads can use the same file at once: a value always gets
    one placeholder, and numbers are never handed out twice. A copy of the
    vault (``copy``, ``deepcopy``, or pickling) opens the same file and
    session, so it shares the data rather than duplicating it.

    The file holds the real values in plain text. It is created readable and
    writable by its owner only; keep it on an encrypted disk, and delete old
    sessions with `purge`. Deleted values are overwritten in the file.

    Example:
        >>> vault = SQLiteVault(":memory:", session="chat-1")
        >>> vault.get_or_create("jan.n@example.com", "EMAIL")
        '[EMAIL_1]'
        >>> vault.get_value("[EMAIL_1]")
        'jan.n@example.com'
        >>> vault.close()
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        session: str = "default",
        *,
        timeout: float = 5.0,
    ) -> None:
        """Open (or create) the vault file and a session in it.

        Args:
            path: The database file. It is created if missing. ``":memory:"``
                keeps the database in memory, unshared, for tests.
            session: Which conversation's mappings to use, e.g. a chat or
                Claude Code session id.
            timeout: Seconds to wait for another process's write to finish
                before failing with ``sqlite3.OperationalError``.

        Raises:
            ValueError: If ``session`` is not a non-empty string, or the file
                was written by a newer version of this library.
        """
        if not isinstance(session, str) or not session:
            raise ValueError("session must be a non-empty string")
        name = os.fsdecode(path)
        # Absolute, so a pickled or copied vault finds the same file.
        self._path = name if name == ":memory:" else os.path.abspath(name)
        self._session = session
        self._timeout = timeout
        self._open(create=True)

    def _open(self, *, create: bool) -> None:
        self._key = _encode(self._session)
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._batch_depth = 0
        self._batch_wrote = False
        self._reset_cache()
        if self._path != ":memory:":
            if not create and not os.path.exists(self._path):
                raise FileNotFoundError(f"The vault file {self._path} is gone")
            # Create the file owner-only before SQLite does (with 0644).
            os.close(os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600))
        self._connect()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            self._path,
            timeout=self._timeout,
            isolation_level=None,  # transactions are explicit
            check_same_thread=False,  # guarded by self._lock
        )
        try:
            self._initialize()
        except BaseException:
            self._conn.close()
            raise

    def _reset_cache(self) -> None:
        """Empty the cache of this session's rows; the next read reloads it."""
        self._placeholder_by_value: dict[str, str] = {}
        self._value_by_placeholder: dict[str, str] = {}
        self._remembered_values: dict[str, tuple[str, str]] = {}
        self._merged_placeholders: set[str] = set()
        self._generation: int | None = _STALE
        self._last_ids: dict[str, int] = dict.fromkeys(_SESSION_TABLES, 0)
        self._data_version = -1  # what PRAGMA data_version said at the last read

    def _initialize(self) -> None:
        self._check_schema_version()
        self._use_wal()
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA secure_delete=ON")  # overwrite deleted values
        with self._write():
            for statement in _SCHEMA:
                self._conn.execute(statement)
            self._conn.execute(
                "INSERT OR IGNORE INTO vault_meta VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO vault_meta VALUES ('generation', 0)"
            )
            self._check_schema_version()  # another process may have made it
            self._touch()
        self._sync()  # load the session outside the write lock

    def _check_schema_version(self) -> None:
        """Refuse a file from a newer version, before changing anything in it."""
        try:
            row = self._conn.execute(
                "SELECT value FROM vault_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:  # no vault_meta yet: a new file
            return
        if row is not None and row[0] != _SCHEMA_VERSION:
            raise ValueError(
                f"{self._path} has vault schema version {row[0]}; this "
                f"version of the library reads version {_SCHEMA_VERSION}"
            )

    # -- Vault protocol -------------------------------------------------------

    def get_or_create(self, value: str, entity_type: str) -> str:
        """Return the placeholder for ``value``, creating one if it is new.

        Args:
            value: The original text. Must be a non-empty string.
            entity_type: Type to use if the value is new. Ignored if the value
                is already stored, so the first type wins.

        Returns:
            The placeholder, e.g. ``"[EMAIL_1]"``.

        Raises:
            ValueError: If ``value`` is empty or ``entity_type`` is invalid.
        """
        if not isinstance(value, str) or not value:
            raise ValueError("Vault values must be non-empty strings")
        with self._lock:
            self._sync()
            existing = self._placeholder_by_value.get(value)
            if existing is not None:
                return existing
            validate_entity_type(entity_type)
            with self._write():
                self._catch_up()  # another process may have stored it
                existing = self._placeholder_by_value.get(value)
                if existing is not None:
                    return existing
                self._touch()
                row = self._conn.execute(
                    "SELECT number FROM vault_counters"
                    " WHERE session = ? AND entity_type = ?",
                    (self._key, entity_type),
                ).fetchone()
                number = (row[0] if row else 0) + 1
                placeholder = format_placeholder(entity_type, number)
                row_id = self._conn.execute(
                    "INSERT INTO vault_values (session, placeholder, value)"
                    " VALUES (?, ?, ?)",
                    (self._key, placeholder, _encode(value)),
                ).lastrowid
                self._conn.execute(
                    "INSERT INTO vault_counters VALUES (?, ?, ?) ON CONFLICT"
                    " (session, entity_type) DO UPDATE SET number = excluded.number",
                    (self._key, entity_type, number),
                )
                # Caught up and holding the write lock: no row can be missing.
                # If the transaction fails, _write throws the cache away.
                self._placeholder_by_value[value] = placeholder
                self._value_by_placeholder[placeholder] = value
                self._last_ids["vault_values"] = row_id or 0
            return placeholder

    def get_placeholder(self, value: str) -> str | None:
        """Return the placeholder for ``value``, or ``None`` if unknown."""
        with self._lock:
            self._sync()
            return self._placeholder_by_value.get(value)

    def get_value(self, placeholder: str) -> str | None:
        """Return the original value for ``placeholder``, or ``None`` if unknown."""
        with self._lock:
            self._sync()
            return self._value_by_placeholder.get(placeholder)

    def items(self) -> list[tuple[str, str]]:
        """Return every ``(placeholder, value)`` pair in creation order."""
        with self._lock:
            self._sync()
            return list(self._value_by_placeholder.items())

    def clear(self) -> None:
        """Forget this session's mappings and restart its numbering at 1.

        Other sessions in the file are not touched.
        """
        with self._write():
            self._catch_up()
            self._delete_sessions([self._key])
            self._touch()
            self._refresh()
        self._checkpoint()

    def __len__(self) -> int:
        """Return the number of values stored in this session."""
        with self._lock:
            self._sync()
            return len(self._value_by_placeholder)

    # -- Remembered values (see vault.base._Remembering) ----------------------

    def _remember(self, value: str, entity_type: str, placeholder: str) -> None:
        """Remember ``value`` for the leak check (see `Vault` and the masker).

        Ignored when ``value`` is stored in its own right, is already
        remembered, or ``placeholder`` is unknown.
        """
        with self._lock:
            self._sync()
            if not self._rememberable(value, placeholder):
                return
            with self._write():
                self._catch_up()
                if self._rememberable(value, placeholder):
                    self._touch()
                    row_id = self._conn.execute(
                        "INSERT INTO vault_remembered"
                        " (session, value, entity_type, placeholder)"
                        " VALUES (?, ?, ?, ?)",
                        (self._key, _encode(value), entity_type, placeholder),
                    ).lastrowid
                    self._remembered_values[value] = (entity_type, placeholder)
                    self._last_ids["vault_remembered"] = row_id or 0

    def _rememberable(self, value: str, placeholder: str) -> bool:
        return (
            placeholder in self._value_by_placeholder
            and value not in self._placeholder_by_value
            and value not in self._remembered_values
        )

    def _remembered(self) -> list[tuple[str, str, str]]:
        """Return every remembered ``(value, entity_type, placeholder)``."""
        with self._lock:
            self._sync()
            return [(v, t, p) for v, (t, p) in self._remembered_values.items()]

    def _mark_merged(self, placeholder: str) -> None:
        """Record that ``placeholder`` covers several overlapping matches."""
        with self._lock:
            self._sync()
            if (
                placeholder in self._merged_placeholders
                or placeholder not in self._value_by_placeholder
            ):
                return
            with self._write():
                self._catch_up()
                if (
                    placeholder in self._value_by_placeholder
                    and placeholder not in self._merged_placeholders
                ):
                    row_id = self._conn.execute(
                        "INSERT INTO vault_merged (session, placeholder) VALUES (?, ?)",
                        (self._key, placeholder),
                    ).lastrowid
                    self._merged_placeholders.add(placeholder)
                    self._last_ids["vault_merged"] = row_id or 0

    def _merged(self) -> set[str]:
        """Return the placeholders that cover several overlapping matches."""
        with self._lock:
            self._sync()
            return set(self._merged_placeholders)

    @contextmanager
    def _batch(self) -> Iterator[None]:
        """Group calls: check the file once, and make every write in one transaction.

        The masker and restorer wrap each call in this. The write lock is
        taken at the first write and released when the block ends.
        """
        with self._lock:
            if not self._batch_depth:
                self._sync()
                self._batch_wrote = False
            self._batch_depth += 1
            try:
                yield
            except BaseException:
                self._batch_depth -= 1
                if not self._batch_depth and self._batch_wrote:
                    self._abort()
                raise
            self._batch_depth -= 1
            if not self._batch_depth and self._conn.in_transaction:
                try:
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._abort()
                    raise

    # -- Sessions --------------------------------------------------------------

    @property
    def session(self) -> str:
        """The session this vault reads and writes."""
        return self._session

    def purge(self, older_than: timedelta) -> int:
        """Delete every session in the file not used for ``older_than``.

        A session is used when a vault is opened on it or stores something in
        it. This vault's own session is deleted too if it is that old, which
        empties it like `clear`.

        Args:
            older_than: How long a session may go unused before it is deleted.

        Returns:
            How many sessions were deleted.

        Raises:
            TypeError: If ``older_than`` is not a `datetime.timedelta`.
            ValueError: If ``older_than`` is negative.
        """
        if not isinstance(older_than, timedelta):
            raise TypeError("older_than must be a datetime.timedelta")
        if older_than < timedelta(0):
            raise ValueError("older_than must not be negative")
        cutoff = time.time() - older_than.total_seconds()
        with self._write():
            self._catch_up()
            stale = [
                key
                for (key,) in self._conn.execute(
                    "SELECT session FROM vault_sessions WHERE last_used < ?",
                    (cutoff,),
                )
            ]
            self._delete_sessions(stale)
            self._conn.executemany(
                "DELETE FROM vault_sessions WHERE session = ?",
                [(key,) for key in stale],
            )
            self._refresh()
        if stale:
            self._checkpoint()
        return len(stale)

    # -- File handling ---------------------------------------------------------

    def close(self) -> None:
        """Close the database connection. The vault can't be used afterwards."""
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:
        """Close the connection when the vault is garbage-collected.

        A `Shield` built on a vault has no ``close()`` of its own, so a vault
        made per request would otherwise leave its connection open. A vault
        inherited across fork() leaves the parent's connection alone.
        """
        conn = getattr(self, "_conn", None)
        if conn is not None and getattr(self, "_pid", None) == os.getpid():
            with suppress(Exception):  # interpreter shutdown, or already closed
                conn.close()

    def __enter__(self) -> SQLiteVault:
        """Return the vault; `close` it when the block ends."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the vault."""
        self.close()

    def __getstate__(self) -> dict[str, object]:
        """Pickle where the data lives, not a copy of it.

        Raises:
            TypeError: For an in-memory vault, whose data would be lost.
        """
        if self._path == ":memory:":
            raise TypeError("An in-memory SQLiteVault can't be pickled or copied")
        return {"path": self._path, "session": self._session, "timeout": self._timeout}

    def __setstate__(self, state: dict[str, object]) -> None:
        """Reopen the same file and session.

        Raises:
            FileNotFoundError: If the file no longer exists.
        """
        self._path = str(state["path"])
        self._session = str(state["session"])
        timeout = state["timeout"]
        self._timeout = float(timeout) if isinstance(timeout, (int, float)) else 5.0
        self._open(create=False)

    def __repr__(self) -> str:
        """Summarize the vault without revealing any stored values."""
        try:
            size = len(self)
        except sqlite3.ProgrammingError:
            return f"{type(self).__name__}(<closed>)"
        return f"{type(self).__name__}(<{size} values>)"

    # -- Internals ---------------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[None]:
        """Run the block in a transaction that holds the database's write lock.

        Inside `_batch`, the batch's transaction is used (and started if need
        be). If the block or the commit fails, the cache is thrown away: it may
        hold changes the database rolled back.
        """
        with self._lock:
            self._check_fork()
            if self._batch_depth:
                if not self._conn.in_transaction:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._batch_wrote = True
                yield  # an error propagates to _batch, which aborts
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._abort()
                raise

    def _abort(self) -> None:
        """Roll back if a transaction is still open, and drop the cache."""
        if self._conn.in_transaction:
            with suppress(sqlite3.Error):  # keep the original error
                self._conn.execute("ROLLBACK")
        self._reset_cache()

    def _check_fork(self) -> None:
        """After fork(), leave the parent's connection alone and open our own."""
        if self._pid != os.getpid():
            _INHERITED.append(self._conn)
            self._pid = os.getpid()
            self._batch_depth = 0
            self._reset_cache()
            self._connect()

    def _use_wal(self) -> None:
        """Switch the file to write-ahead logging, so readers don't block writers.

        The switch needs the file to itself and doesn't wait for a busy one,
        so when several processes open a new file at once, retry until one of
        them has made it or the timeout passes.
        """
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                (mode,) = self._conn.execute("PRAGMA journal_mode").fetchone()
                if mode in ("wal", "memory"):
                    return
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    def _checkpoint(self) -> None:
        """Copy the write-ahead log into the file and empty it.

        After a delete, so older copies of the deleted values don't linger in
        the log. It does nothing while another connection is reading.
        """
        with self._lock, suppress(sqlite3.OperationalError):
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _query_data_version(self) -> int:
        (version,) = self._conn.execute("PRAGMA data_version").fetchone()
        return int(version)

    def _sync(self) -> None:
        """Bring the cache up to date if another connection changed the file.

        Inside `_batch` this was done once, when the batch started.
        """
        self._check_fork()
        if self._batch_depth:
            return
        if self._query_data_version() != self._data_version:
            self._conn.execute("BEGIN")  # one consistent snapshot
            try:
                self._catch_up()
            finally:
                self._conn.execute("COMMIT")

    def _catch_up(self) -> None:
        """`_refresh`, if anything changed since the last read.

        Must run inside a transaction. This connection's own commits don't
        change ``data_version``, so they must update the cache themselves.
        """
        version = self._query_data_version()
        if version != self._data_version:
            self._refresh()
            self._data_version = version  # only once the refresh is complete

    def _refresh(self) -> None:
        """Read this session's rows added since the last refresh.

        Must run inside a transaction. When the session was cleared or purged
        since (its generation changed), the cache starts over.
        """
        row = self._conn.execute(
            "SELECT generation FROM vault_sessions WHERE session = ?", (self._key,)
        ).fetchone()
        generation = row[0] if row else None
        if generation != self._generation:
            self._reset_cache()
            self._generation = generation
        for row_id, placeholder, data in self._new_rows(
            "vault_values", "placeholder, value"
        ):
            value = _decode(data)
            self._placeholder_by_value[value] = placeholder
            self._value_by_placeholder[placeholder] = value
            self._last_ids["vault_values"] = row_id
        for row_id, data, entity_type, placeholder in self._new_rows(
            "vault_remembered", "value, entity_type, placeholder"
        ):
            self._remembered_values.setdefault(
                _decode(data), (entity_type, placeholder)
            )
            self._last_ids["vault_remembered"] = row_id
        for row_id, placeholder in self._new_rows("vault_merged", "placeholder"):
            self._merged_placeholders.add(placeholder)
            self._last_ids["vault_merged"] = row_id

    def _new_rows(self, table: str, columns: str) -> sqlite3.Cursor:
        return self._conn.execute(
            f"SELECT id, {columns} FROM {table}"
            " WHERE session = ? AND id > ? ORDER BY id",
            (self._key, self._last_ids[table]),
        )

    def _touch(self) -> None:
        """Mark this session used now, creating it (with a new generation).

        Must run inside a write transaction.
        """
        now = time.time()
        exists = self._conn.execute(
            "UPDATE vault_sessions SET last_used = ? WHERE session = ?",
            (now, self._key),
        ).rowcount
        if not exists:
            self._conn.execute(
                "INSERT INTO vault_sessions VALUES (?, ?, ?)",
                (self._key, self._next_generation(), now),
            )

    def _next_generation(self) -> int:
        self._conn.execute(
            "UPDATE vault_meta SET value = value + 1 WHERE key = 'generation'"
        )
        (generation,) = self._conn.execute(
            "SELECT value FROM vault_meta WHERE key = 'generation'"
        ).fetchone()
        return int(generation)

    def _delete_sessions(self, keys: list[bytes]) -> None:
        """Delete these sessions' rows and give them a new generation.

        Every cache of such a session, in any process, then starts over.
        """
        for table in _SESSION_TABLES:
            self._conn.executemany(
                f"DELETE FROM {table} WHERE session = ?",
                [(key,) for key in keys],
            )
        for key in keys:
            self._conn.execute(
                "UPDATE vault_sessions SET generation = ? WHERE session = ?",
                (self._next_generation(), key),
            )
