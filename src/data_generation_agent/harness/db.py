from __future__ import annotations

import itertools
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_BUSY_TIMEOUT_MS = 5_000
_TRANSACTION_MODES = frozenset({"DEFERRED", "IMMEDIATE", "EXCLUSIVE"})
_SAVEPOINT_IDS = itertools.count(1)


class DatabaseConfigurationError(RuntimeError):
    """SQLite could not be configured with the Harness safety settings."""


def connect_database(
    path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a Harness SQLite database with explicit transaction semantics.

    Persistent databases use WAL. ``:memory:`` remains supported for small unit
    tests, although SQLite itself reports its journal mode as ``memory`` there.
    Callers own the returned connection and must close it.
    """

    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
        raise TypeError("busy_timeout_ms must be an integer")
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be non-negative")

    raw_path = str(path)
    persistent = raw_path != ":memory:"
    if persistent:
        database_path = Path(path).expanduser()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path = str(database_path)

    connection = sqlite3.connect(
        raw_path,
        timeout=max(busy_timeout_ms / 1_000, 0.001),
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()

        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        configured_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        if foreign_keys != 1:
            raise DatabaseConfigurationError("SQLite foreign key enforcement is disabled")
        if configured_timeout != busy_timeout_ms:
            raise DatabaseConfigurationError(
                f"SQLite busy_timeout mismatch: {configured_timeout} != {busy_timeout_ms}"
            )
        if persistent and journal_mode != "wal":
            raise DatabaseConfigurationError(
                f"persistent Harness database did not enter WAL mode: {journal_mode}"
            )
    except Exception:
        connection.close()
        raise

    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    mode: str = "IMMEDIATE",
) -> Iterator[sqlite3.Connection]:
    """Run a transaction, using a savepoint when already inside one."""

    normalized_mode = mode.upper()
    if normalized_mode not in _TRANSACTION_MODES:
        raise ValueError(f"unsupported SQLite transaction mode: {mode}")

    if connection.in_transaction:
        savepoint = f"harness_sp_{next(_SAVEPOINT_IDS)}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return

    connection.execute(f"BEGIN {normalized_mode}")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize_database(
    path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a database and apply every bundled migration.

    The import stays local so ``db`` remains usable by migration tooling without
    introducing a module-import cycle.
    """

    from .migrations import apply_migrations

    connection = connect_database(path, busy_timeout_ms=busy_timeout_ms)
    try:
        apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return connection
