from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data_generation_agent.harness.db import connect_database, initialize_database, transaction
from data_generation_agent.harness.migrations import (
    Migration,
    MigrationDriftError,
    MigrationError,
    applied_migrations,
    apply_migrations,
    current_schema_version,
)
from data_generation_agent.harness.state import (
    ATTEMPT_TRANSITIONS,
    CANDIDATE_TRANSITIONS,
    JOB_TRANSITIONS,
)


NOW = "2026-07-11T12:00:00Z"
HASH_A = "a" * 64
HASH_B = "b" * 64

EXPECTED_TABLES = {
    "schema_migrations",
    "jobs",
    "candidates",
    "revisions",
    "attempts",
    "events",
    "artifacts",
    "leases",
    "budgets",
    "trials",
    "outbox",
    "knowledge_snapshots",
    "knowledge_chunks",
    "persona_snapshots",
    "prompt_compilations",
    "passrate_trials",
    "passrate_results",
}


def _transition_states(transitions: dict[str, set[str]]) -> set[str]:
    return set(transitions) | {
        target
        for targets in transitions.values()
        for target in targets
    }


def _seed_attempt(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, job_type, status, policy_digest, config_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("job-1", "review", "PENDING", HASH_A, "{}", NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO candidates(
            candidate_id, job_id, source_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("candidate-1", "job-1", "source-1", "PENDING", NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO revisions(
            revision_id, candidate_id, revision_number, content_hash,
            payload_json, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("revision-1", "candidate-1", 1, HASH_B, '{"question":"q"}', "ACTIVE", NOW),
    )
    connection.execute(
        """
        INSERT INTO attempts(
            attempt_id, revision_id, stage_id, tool_id, tool_version,
            input_fingerprint, attempt_number, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "attempt-1",
            "revision-1",
            "quality_review",
            "lao_quality_review",
            "1.0.0",
            HASH_A,
            1,
            "PENDING",
            NOW,
            NOW,
        ),
    )


def test_initialize_database_configures_pragmas_and_all_tables(tmp_path: Path) -> None:
    database = tmp_path / "state" / "harness.sqlite3"
    connection = initialize_database(database, busy_timeout_ms=7_500)
    try:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 7_500

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert EXPECTED_TABLES <= tables
        assert current_schema_version(connection) == 4
        assert [row.version for row in applied_migrations(connection)] == [1, 2, 3, 4]
        assert apply_migrations(connection) == []
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 4
    finally:
        connection.close()


def test_foreign_keys_unique_json_and_business_checks_are_enforced(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "constraints.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, job_id, status, created_at, updated_at
                ) VALUES ('orphan', 'missing-job', 'PENDING', ?, ?)
                """,
                (NOW, NOW),
            )

        _seed_attempt(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO revisions(
                    revision_id, candidate_id, revision_number, content_hash,
                    payload_json, status, created_at
                ) VALUES ('revision-duplicate', 'candidate-1', 1, ?, '{}', 'DRAFT', ?)
                """,
                ("c" * 64, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE revisions SET payload_json = 'not-json' WHERE revision_id = 'revision-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO budgets(
                    job_id, budget_type, unit, limit_amount,
                    reserved_amount, consumed_amount, updated_at
                ) VALUES ('job-1', 'tokens', 'token', 10, 7, 4, ?)
                """,
                (NOW,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO trials(
                    trial_id, attempt_id, trial_index, status, valid, passed,
                    created_at, updated_at
                ) VALUES ('trial-1', 'attempt-1', 0, 'COMPLETED', 0, 1, ?, ?)
                """,
                (NOW, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO outbox(
                    outbox_id, job_id, aggregate_type, aggregate_id, destination,
                    operation, dedupe_key, payload_json, payload_hash, status,
                    available_at, created_at, updated_at
                ) VALUES (
                    'outbox-1', 'job-1', 'candidate', 'candidate-1', 'feishu',
                    'PATCH', 'dedupe-1', '{}', ?, 'SENT', ?, ?, ?
                )
                """,
                (HASH_A, NOW, NOW, NOW),
            )
    finally:
        connection.close()


def test_events_artifacts_and_migration_history_are_immutable(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "immutable.sqlite3")
    try:
        _seed_attempt(connection)
        connection.execute(
            """
            INSERT INTO events(
                event_key, job_id, aggregate_type, aggregate_id,
                event_type, payload_json, created_at
            ) VALUES ('event-key-1', 'job-1', 'job', 'job-1', 'JOB_CREATED', '{}', ?)
            """,
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, job_id, revision_id, attempt_id, kind, sha256,
                uri, media_type, size_bytes, metadata_json, created_at
            ) VALUES (
                'artifact-1', 'job-1', 'revision-1', 'attempt-1', 'model_response', ?,
                'artifacts/aa/file.json', 'application/json', 2, '{}', ?
            )
            """,
            (HASH_A, NOW),
        )

        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            connection.execute("UPDATE events SET event_type = 'CHANGED' WHERE event_key = 'event-key-1'")
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            connection.execute("DELETE FROM events WHERE event_key = 'event-key-1'")
        with pytest.raises(sqlite3.IntegrityError, match="artifacts are immutable"):
            connection.execute("UPDATE artifacts SET size_bytes = 3 WHERE artifact_id = 'artifact-1'")
        with pytest.raises(sqlite3.IntegrityError, match="artifacts are immutable"):
            connection.execute("DELETE FROM artifacts WHERE artifact_id = 'artifact-1'")
        with pytest.raises(sqlite3.IntegrityError, match="migration history is immutable"):
            connection.execute("DELETE FROM schema_migrations WHERE version = 1")
    finally:
        connection.close()


def test_schema_accepts_every_explicit_state_machine_status(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "state-graph.sqlite3")
    try:
        _seed_attempt(connection)
        for status in sorted(_transition_states(JOB_TRANSITIONS)):
            connection.execute("UPDATE jobs SET status = ? WHERE job_id = 'job-1'", (status,))
        for status in sorted(_transition_states(CANDIDATE_TRANSITIONS)):
            connection.execute(
                "UPDATE candidates SET status = ? WHERE candidate_id = 'candidate-1'",
                (status,),
            )
        for version, status in enumerate(sorted(_transition_states(ATTEMPT_TRANSITIONS)), 1):
            connection.execute(
                "UPDATE attempts SET status = ?, state_version = ? WHERE attempt_id = 'attempt-1'",
                (status, version),
            )
        assert connection.execute(
            "SELECT state_version FROM attempts WHERE attempt_id = 'attempt-1'"
        ).fetchone()[0] == len(_transition_states(ATTEMPT_TRANSITIONS))
    finally:
        connection.close()


def test_failed_migration_rolls_back_schema_and_version_record(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "failed-migration.sqlite3")
    broken = Migration(
        version=2,
        name="broken",
        sql="""
        CREATE TABLE must_rollback (id INTEGER PRIMARY KEY);
        INSERT INTO table_that_does_not_exist(value) VALUES (1);
        """,
    )
    try:
        with pytest.raises(MigrationError, match="failed to apply migration 2_broken"):
            apply_migrations(connection, [broken])
        assert current_schema_version(connection) == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'must_rollback'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        connection.close()


def test_applied_migration_checksum_drift_is_rejected(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "drift.sqlite3")
    original = Migration(7, "example", "CREATE TABLE example (id INTEGER PRIMARY KEY);")
    changed = Migration(7, "example", "CREATE TABLE example (id TEXT PRIMARY KEY);")
    try:
        assert apply_migrations(connection, [original]) == [7]
        assert apply_migrations(connection, [original]) == []
        with pytest.raises(MigrationDriftError, match="version 7"):
            apply_migrations(connection, [changed])
        assert current_schema_version(connection) == 7
        assert connection.execute("PRAGMA table_info(example)").fetchone()["type"] == "INTEGER"
    finally:
        connection.close()


def test_nested_transaction_uses_savepoint_and_preserves_outer_work(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "savepoint.sqlite3")
    try:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, job_type, status, policy_digest, config_json, created_at, updated_at
                ) VALUES ('outer-job', 'review', 'PENDING', ?, '{}', ?, ?)
                """,
                (HASH_A, NOW, NOW),
            )
            with pytest.raises(sqlite3.IntegrityError):
                with transaction(connection):
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            job_id, job_type, status, policy_digest, config_json, created_at, updated_at
                        ) VALUES ('bad-job', 'review', 'UNKNOWN', ?, '{}', ?, ?)
                        """,
                        (HASH_A, NOW, NOW),
                    )
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE job_id = 'outer-job'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE job_id = 'bad-job'").fetchone()[0] == 0
    finally:
        connection.close()
