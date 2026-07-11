from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_generation_agent.harness.budgets import (
    BudgetExceededError,
    BudgetManager,
    InsufficientReservationError,
)
from data_generation_agent.harness.db import connect_database, initialize_database
from data_generation_agent.harness.migrations import apply_migrations


NOW = "2026-07-11T06:00:00.000000Z"


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 11, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def insert_job(connection: sqlite3.Connection, job_id: str = "job-1") -> None:
    connection.execute(
        "INSERT INTO jobs "
        "(job_id, job_type, status, policy_digest, config_json, created_at, updated_at) "
        "VALUES (?, 'AUDIT', 'PENDING', ?, '{}', ?, ?)",
        (job_id, "b" * 64, NOW, NOW),
    )


def test_budget_reserve_consume_release_are_atomic_and_non_negative() -> None:
    connection = initialize_database(":memory:")
    clock = FakeClock()
    try:
        insert_job(connection)
        manager = BudgetManager(connection, clock=clock)
        initial = manager.create("job-1", "tokens", "token", 10)
        assert initial.available_amount == 10

        clock.advance(1)
        reserved = manager.reserve("job-1", "tokens", 6)
        assert reserved.reserved_amount == 6
        assert reserved.consumed_amount == 0
        assert reserved.available_amount == 4

        clock.advance(1)
        consumed = manager.consume("job-1", "tokens", 4)
        assert consumed.reserved_amount == 2
        assert consumed.consumed_amount == 4
        assert consumed.available_amount == 4

        released = manager.release("job-1", "tokens", 2)
        assert released.reserved_amount == 0
        assert released.consumed_amount == 4
        assert released.available_amount == 6
        assert released.updated_at > initial.updated_at

        row = connection.execute(
            "SELECT reserved_amount, consumed_amount FROM budgets "
            "WHERE job_id = 'job-1' AND budget_type = 'tokens'"
        ).fetchone()
        assert row["reserved_amount"] >= 0
        assert row["consumed_amount"] >= 0
    finally:
        connection.close()


def test_failed_budget_operations_leave_amounts_unchanged() -> None:
    connection = initialize_database(":memory:")
    try:
        insert_job(connection)
        manager = BudgetManager(connection, clock=FakeClock())
        manager.create("job-1", "cost", "usd", 5)
        manager.reserve("job-1", "cost", 4)

        with pytest.raises(BudgetExceededError):
            manager.reserve("job-1", "cost", 2)
        with pytest.raises(InsufficientReservationError):
            manager.consume("job-1", "cost", 5)
        with pytest.raises(InsufficientReservationError):
            manager.release("job-1", "cost", 5)

        budget = manager.get("job-1", "cost")
        assert budget.reserved_amount == 4
        assert budget.consumed_amount == 0
        assert budget.available_amount == 1

        for invalid in (-1, 0, float("nan"), float("inf")):
            with pytest.raises((TypeError, ValueError)):
                manager.reserve("job-1", "cost", invalid)
        assert manager.get("job-1", "cost") == budget
    finally:
        connection.close()


def test_concurrent_reservations_cannot_overspend(tmp_path: Path) -> None:
    database_path = tmp_path / "budget-race.sqlite3"
    setup = connect_database(database_path)
    apply_migrations(setup)
    insert_job(setup)
    BudgetManager(setup, clock=FakeClock()).create("job-1", "requests", "request", 10)
    setup.close()
    barrier = threading.Barrier(2)

    def reserve(owner: str) -> str:
        del owner
        connection = connect_database(database_path)
        try:
            manager = BudgetManager(
                connection,
                clock=lambda: datetime(2026, 7, 11, 6, 0, tzinfo=timezone.utc),
            )
            barrier.wait(timeout=5)
            try:
                manager.reserve("job-1", "requests", 7)
            except BudgetExceededError:
                return "exceeded"
            return "reserved"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ["worker-a", "worker-b"]))

    assert sorted(outcomes) == ["exceeded", "reserved"]
    check = connect_database(database_path)
    try:
        budget = BudgetManager(check).get("job-1", "requests")
        assert budget.reserved_amount == 7
        assert budget.consumed_amount == 0
        assert budget.available_amount == 3
    finally:
        check.close()
