from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .db import transaction


_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    checksum TEXT NOT NULL CHECK (
        length(checksum) = 64
        AND checksum NOT GLOB '*[^0-9a-f]*'
    ),
    applied_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
)
""".strip()


class MigrationError(RuntimeError):
    """A schema migration could not be validated or applied."""


class MigrationDriftError(MigrationError):
    """An applied migration no longer matches its immutable source."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("migration version must be a positive integer")
        if not self.name.strip():
            raise ValueError("migration name must not be empty")
        if not self.sql.strip():
            raise ValueError("migration SQL must not be empty")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @classmethod
    def from_file(cls, version: int, name: str, path: Path) -> "Migration":
        try:
            sql = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MigrationError(f"cannot read migration file: {path}: {exc}") from exc
        return cls(version=version, name=name, sql=sql)


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str


def bundled_migrations() -> tuple[Migration, ...]:
    return (
        Migration.from_file(1, "initial", _MIGRATIONS_DIR / "0001_initial.sql"),
        Migration.from_file(2, "feishu_ingestion", _MIGRATIONS_DIR / "0002_feishu_ingestion.sql"),
        Migration.from_file(3, "knowledge_prompt", _MIGRATIONS_DIR / "0003_knowledge_prompt.sql"),
        Migration.from_file(4, "passrate", _MIGRATIONS_DIR / "0004_passrate.sql"),
        Migration.from_file(5, "generation", _MIGRATIONS_DIR / "0005_generation.sql"),
    )


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(_MIGRATION_TABLE_SQL)


def _ordered_migrations(migrations: Iterable[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions: set[int] = set()
    names: set[str] = set()
    for migration in ordered:
        if migration.version in versions:
            raise MigrationError(f"duplicate migration version: {migration.version}")
        if migration.name in names:
            raise MigrationError(f"duplicate migration name: {migration.name}")
        versions.add(migration.version)
        names.add(migration.name)
    return ordered


def _iter_sql_statements(sql: str) -> Iterator[str]:
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration contains an incomplete SQL statement")


def applied_migrations(connection: sqlite3.Connection) -> list[AppliedMigration]:
    _ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version, name, checksum, applied_at "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [
        AppliedMigration(
            version=int(row["version"]),
            name=str(row["name"]),
            checksum=str(row["checksum"]),
            applied_at=str(row["applied_at"]),
        )
        for row in rows
    ]


def current_schema_version(connection: sqlite3.Connection) -> int:
    _ensure_migration_table(connection)
    row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()
    return int(row["version"])


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> list[int]:
    """Apply pending migrations atomically and verify immutable checksums.

    The metadata table is bootstrapped independently. Every actual migration is
    then executed statement-by-statement inside one ``BEGIN IMMEDIATE`` block;
    this avoids ``sqlite3.executescript``'s implicit-commit behavior.
    """

    _ensure_migration_table(connection)
    ordered = _ordered_migrations(migrations if migrations is not None else bundled_migrations())
    applied_now: list[int] = []

    for migration in ordered:
        try:
            with transaction(connection, mode="IMMEDIATE"):
                existing = connection.execute(
                    "SELECT name, checksum FROM schema_migrations WHERE version = ?",
                    (migration.version,),
                ).fetchone()
                if existing is not None:
                    if existing["name"] != migration.name or existing["checksum"] != migration.checksum:
                        raise MigrationDriftError(
                            "applied migration drift detected for "
                            f"version {migration.version}: expected "
                            f"{existing['name']}@{existing['checksum']}, got "
                            f"{migration.name}@{migration.checksum}"
                        )
                    continue

                for statement in _iter_sql_statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.checksum),
                )
        except MigrationDriftError:
            raise
        except (sqlite3.DatabaseError, MigrationError) as exc:
            raise MigrationError(
                f"failed to apply migration {migration.version}_{migration.name}: {exc}"
            ) from exc
        applied_now.append(migration.version)

    return applied_now
