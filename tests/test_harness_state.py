from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data_generation_agent.harness.db import connect_database, initialize_database
from data_generation_agent.harness.migrations import apply_migrations
from data_generation_agent.harness.state import (
    InvalidTransitionError,
    StateConflictError,
    StateVersionConflictError,
)
from data_generation_agent.harness.store import HarnessStore


NOW = "2026-07-11T04:00:00.000000Z"


def fixed_clock() -> datetime:
    return datetime(2026, 7, 11, 4, 0, tzinfo=timezone.utc)


def insert_job(connection: sqlite3.Connection, job_id: str = "job-1") -> None:
    connection.execute(
        "INSERT INTO jobs "
        "(job_id, job_type, status, policy_digest, config_json, created_at, updated_at) "
        "VALUES (?, 'AUDIT', 'PENDING', ?, '{}', ?, ?)",
        (job_id, "a" * 64, NOW, NOW),
    )


def test_transition_is_cas_and_appends_event_in_same_transaction() -> None:
    connection = initialize_database(":memory:")
    try:
        insert_job(connection)
        store = HarnessStore(connection, clock=fixed_clock)

        result = store.transition(
            "job",
            "job-1",
            "PENDING",
            "RUNNING",
            expected_version=0,
            event_type="JOB_STARTED",
            payload={"worker": "worker-1"},
            event_key="evt-job-started",
        )

        row = connection.execute(
            "SELECT status, state_version, updated_at FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert dict(row) == {
            "status": "RUNNING",
            "state_version": 1,
            "updated_at": NOW,
        }
        assert result.previous_status == "PENDING"
        assert result.new_status == "RUNNING"
        assert result.previous_version == 0
        assert result.new_version == 1

        events = store.list_events("job", "job-1")
        assert len(events) == 1
        assert events[0].event_id == result.event_id
        assert events[0].job_id == "job-1"
        assert events[0].payload == {
            "new_status": "RUNNING",
            "new_version": 1,
            "previous_status": "PENDING",
            "previous_version": 0,
            "worker": "worker-1",
        }
    finally:
        connection.close()


def test_failed_event_append_rolls_back_state_change() -> None:
    connection = initialize_database(":memory:")
    try:
        insert_job(connection)
        store = HarnessStore(connection, clock=fixed_clock)
        store.append_event(
            "job",
            "job-1",
            "SEEDED",
            event_key="duplicate-event-key",
            job_id="job-1",
        )

        with pytest.raises(sqlite3.IntegrityError):
            store.transition(
                "job",
                "job-1",
                "PENDING",
                "RUNNING",
                event_type="JOB_STARTED",
                event_key="duplicate-event-key",
            )

        row = connection.execute(
            "SELECT status, state_version FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert dict(row) == {"status": "PENDING", "state_version": 0}
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        connection.close()


def test_events_are_database_enforced_append_only() -> None:
    connection = initialize_database(":memory:")
    try:
        insert_job(connection)
        store = HarnessStore(connection, clock=fixed_clock)
        event_id = store.append_event(
            "job",
            "job-1",
            "SEEDED",
            event_key="immutable-event",
            job_id="job-1",
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE event_id = ?",
                (json.dumps({"changed": True}), event_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        connection.close()


def test_invalid_transition_and_stale_version_fail_without_events() -> None:
    connection = initialize_database(":memory:")
    try:
        insert_job(connection)
        store = HarnessStore(connection, clock=fixed_clock)

        with pytest.raises(InvalidTransitionError):
            store.transition(
                "job",
                "job-1",
                "PENDING",
                "COMPLETED",
                event_type="ILLEGAL",
            )
        with pytest.raises(StateVersionConflictError):
            store.transition(
                "job",
                "job-1",
                "PENDING",
                "RUNNING",
                expected_version=9,
                event_type="STALE",
            )
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.get_status("job", "job-1") == "PENDING"
    finally:
        connection.close()


def test_concurrent_state_claim_has_one_winner(tmp_path: Path) -> None:
    database_path = tmp_path / "state-race.sqlite3"
    setup = connect_database(database_path)
    apply_migrations(setup)
    insert_job(setup)
    setup.close()

    barrier = threading.Barrier(2)

    def compete(target: str) -> str:
        connection = connect_database(database_path)
        try:
            store = HarnessStore(connection, clock=fixed_clock)
            barrier.wait(timeout=5)
            try:
                store.transition(
                    "job",
                    "job-1",
                    "PENDING",
                    target,
                    expected_version=0,
                    event_type=f"JOB_{target}",
                )
            except StateConflictError:
                return "conflict"
            return "won"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ["RUNNING", "CANCELLED"]))

    assert sorted(outcomes) == ["conflict", "won"]
    check = connect_database(database_path)
    try:
        row = check.execute(
            "SELECT status, state_version FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()
        assert row["status"] in {"RUNNING", "CANCELLED"}
        assert row["state_version"] == 1
        assert check.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        check.close()
