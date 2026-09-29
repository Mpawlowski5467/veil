"""Keeping the gateway's conversations on disk, and building their shields."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from ..detectors.literal import LiteralPlaceholderDetector
from ..detectors.regex import RegexDetector
from ..placeholders import placeholder_type
from ..shield import Shield
from ..vault.sqlite import SQLiteVault
from .config import Settings
from .ledger import canonical
from .openai_request import ResponsesRequestMasker
from .request import DEFAULT_NOTE, RequestMasker
from .server import Session, Sessions

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ledger_texts ("
    " session TEXT NOT NULL, digest TEXT NOT NULL, masked TEXT NOT NULL,"
    " used REAL NOT NULL, PRIMARY KEY (session, digest))",
    "CREATE TABLE IF NOT EXISTS ledger_tools ("
    " session TEXT NOT NULL, tool_use_id TEXT NOT NULL, digest TEXT NOT NULL,"
    " masked TEXT NOT NULL, used REAL NOT NULL, PRIMARY KEY (session, tool_use_id))",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _create_private(path: Path) -> None:
    """Create ``path`` readable and writable by its owner only, if missing."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # made already, maybe by another gateway just now
    os.close(fd)


class SQLiteLedger:
    """A `Ledger` kept in an SQLite file, one conversation per ``session``.

    It holds only what the model wrote (masked text, masked tool inputs) and
    hashes of the restored forms, never a real value.
    """

    def __init__(self, path: str | os.PathLike[str], session: str) -> None:
        """Open (creating if needed) the ledger file for one conversation."""
        self._path = Path(path)
        self._session = session
        self._pid = os.getpid()
        _create_private(self._path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self._path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        for attempt in range(100):
            # Switching to WAL needs a moment alone with a new file; another
            # gateway opening it at the same time makes it wait.
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error) or attempt == 99:
                    raise
                time.sleep(0.02)
        self._db.execute("PRAGMA synchronous=NORMAL")
        for statement in _SCHEMA:
            self._db.execute(statement)

    def close(self) -> None:
        """Close the file."""
        with self._lock:
            self._db.close()

    def __del__(self) -> None:
        """Close the connection when the ledger is dropped (in this process)."""
        db = getattr(self, "_db", None)
        if db is not None and getattr(self, "_pid", None) == os.getpid():
            db.close()

    def record_text(self, restored: str, masked: str) -> None:
        """Remember that ``masked`` model text was restored as ``restored``."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO ledger_texts VALUES (?, ?, ?, ?)",
                (self._session, _digest(restored), masked, time.time()),
            )

    def masked_text(self, restored: str) -> str | None:
        """Return the model's masked text for ``restored``, if recorded."""
        with self._lock:
            row = self._db.execute(
                "SELECT masked FROM ledger_texts WHERE session = ? AND digest = ?",
                (self._session, _digest(restored)),
            ).fetchone()
        return None if row is None else str(row[0])

    def record_tool_input(self, tool_use_id: str, restored: Any, masked: Any) -> None:
        """Remember the masked input of a tool call restored as ``restored``."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO ledger_tools VALUES (?, ?, ?, ?, ?)",
                (
                    self._session,
                    tool_use_id,
                    _digest(canonical(restored)),
                    json.dumps(masked),
                    time.time(),
                ),
            )

    def masked_tool_input(self, tool_use_id: str, restored: Any) -> Any | None:
        """Return the masked input of a tool call, if it still matches."""
        with self._lock:
            row = self._db.execute(
                "SELECT digest, masked FROM ledger_tools"
                " WHERE session = ? AND tool_use_id = ?",
                (self._session, tool_use_id),
            ).fetchone()
        if row is None or row[0] != _digest(canonical(restored)):
            return None
        return json.loads(row[1])

    def forget(self) -> None:
        """Delete this conversation's entries."""
        with self._lock:
            for table in ("ledger_texts", "ledger_tools"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE session = ?",  # two fixed names
                    (self._session,),
                )

    def purge(self, older_than: timedelta) -> int:
        """Delete every conversation's entries not written for ``older_than``."""
        cutoff = time.time() - older_than.total_seconds()
        with self._lock:
            deleted = 0
            for table in ("ledger_texts", "ledger_tools"):
                cursor = self._db.execute(
                    f"DELETE FROM {table} WHERE used < ?",  # two fixed names
                    (cutoff,),
                )
                deleted += cursor.rowcount
        return deleted


def literal_types(settings: Settings, identity: Mapping[str, str]) -> set[str]:
    """The types whose placeholder-shaped text is masked as literal text."""
    detector = RegexDetector(settings.patterns)
    return {*detector.entity_types, *settings.entities, *identity.values()}


def registered_values(
    settings: Settings, identity: Mapping[str, str]
) -> dict[str, str]:
    """Every value registered by the settings or the git identity, by value."""
    values = {
        value: kind for kind, found in settings.entities.items() for value in found
    }
    values.update(identity)
    return values


def shield_factory(
    settings: Settings, vault_path: Path, identity: Mapping[str, str]
) -> Callable[[str], Shield]:
    """Return a function building the shield for one conversation id.

    The set of types escaped as literal text is fixed here, once, so a
    conversation always masks the same text the same way.
    """
    types = literal_types(settings, identity)

    def make(session_id: str) -> Shield:
        vault = SQLiteVault(vault_path, session=session_id)
        # The types the conversation already uses count too, so literal text
        # stays literal even if a pattern was since removed from the settings.
        used = {placeholder_type(p) for p, _ in vault.items()}
        literal = LiteralPlaceholderDetector(types | {t for t in used if t})
        shield = Shield(
            detectors=[literal, RegexDetector(settings.patterns)],
            vault=vault,
            redact_warnings=True,
        )
        for entity_type, values in settings.entities.items():
            for value in values:
                shield.add_entity(value, entity_type)
        for value, entity_type in identity.items():
            # The email too: a local address like jan@corp has no pattern.
            shield.add_entity(value, entity_type)
        return shield

    return make


def open_sessions(
    data_dir: Path,
    settings: Settings,
    identity: Mapping[str, str],
    *,
    api: Literal["anthropic", "openai"] = "anthropic",
) -> Sessions:
    """Open the conversations kept in ``data_dir``, purging old ones first."""
    vault_path = data_dir / "vault.db"
    ledger_path = data_dir / "ledger.db"
    retention = timedelta(days=settings.retention_days)
    maintenance = SQLiteVault(vault_path, session="_maintenance")
    try:
        maintenance.purge(retention)
    finally:
        maintenance.close()
    ledger = SQLiteLedger(ledger_path, "_maintenance")
    try:
        ledger.purge(retention)
    finally:
        ledger.close()
    note = DEFAULT_NOTE if settings.note else None

    registered = registered_values(settings, identity)

    def make_session(session_id: str) -> Session:
        shield = make_shield(session_id)
        session_ledger = SQLiteLedger(ledger_path, session_id)
        masker_type = ResponsesRequestMasker if api == "openai" else RequestMasker
        masker = masker_type(shield, session_ledger, note=note, registered=registered)
        return Session(shield, session_ledger, masker)

    make_shield = shield_factory(settings, vault_path, identity)
    return Sessions(make_session=make_session)
