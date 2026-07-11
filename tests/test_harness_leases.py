from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_generation_agent.harness.db import connect_database, initialize_database
from data_generation_agent.harness.leases import (
    LeaseBusyError,
    LeaseManager,
    StaleLeaseTokenError,
)
from data_generation_agent.harness.migrations import apply_migrations


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_expired_lease_is_recovered_with_new_fencing_token() -> None:
    connection = initialize_database(":memory:")
    clock = FakeClock()
    tokens = iter(["lease-old", "lease-new"])
    manager = LeaseManager(connection, clock=clock, token_factory=lambda: next(tokens))
    try:
        first = manager.acquire("attempt", "attempt-1", "worker-a", 10)
        assert first.lease_token == "lease-old"
        assert manager.is_valid("attempt", "attempt-1", first.lease_token)

        with pytest.raises(LeaseBusyError):
            manager.acquire("attempt", "attempt-1", "worker-b", 10)

        clock.advance(10)
        replacement = manager.acquire("attempt", "attempt-1", "worker-b", 20)
        assert replacement.lease_token == "lease-new"
        assert replacement.state_version == 1
        assert not manager.is_valid("attempt", "attempt-1", first.lease_token)
        assert manager.is_valid("attempt", "attempt-1", replacement.lease_token)

        with pytest.raises(StaleLeaseTokenError):
            manager.renew("attempt", "attempt-1", first.lease_token, 10)
        with pytest.raises(StaleLeaseTokenError):
            manager.release("attempt", "attempt-1", first.lease_token)
    finally:
        connection.close()


def test_renew_release_and_reclaim_use_injected_clock() -> None:
    connection = initialize_database(":memory:")
    clock = FakeClock()
    manager = LeaseManager(
        connection,
        clock=clock,
        token_factory=lambda: "lease-fixed",
    )
    try:
        lease = manager.acquire("job", "job-1", "worker-a", 5)
        original_expiry = lease.expires_at
        clock.advance(2)
        renewed = manager.renew("job", "job-1", lease.lease_token, 10)
        assert renewed.expires_at > original_expiry
        assert renewed.state_version == 1

        manager.release("job", "job-1", lease.lease_token)
        assert not manager.is_valid("job", "job-1", lease.lease_token)

        manager = LeaseManager(
            connection,
            clock=clock,
            token_factory=lambda: "lease-expiring",
        )
        expiring = manager.acquire("job", "job-2", "worker-a", 1)
        clock.advance(2)
        assert manager.reclaim_expired() == 1
        assert not manager.is_valid("job", "job-2", expiring.lease_token)
        assert connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    finally:
        connection.close()


def test_concurrent_lease_acquire_has_one_owner(tmp_path: Path) -> None:
    database_path = tmp_path / "lease-race.sqlite3"
    setup = connect_database(database_path)
    apply_migrations(setup)
    setup.close()
    barrier = threading.Barrier(2)

    def compete(owner: str) -> str:
        connection = connect_database(database_path)
        try:
            manager = LeaseManager(
                connection,
                clock=lambda: datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc),
                token_factory=lambda: f"token-{owner}",
            )
            barrier.wait(timeout=5)
            try:
                manager.acquire("attempt", "attempt-race", owner, 30)
            except LeaseBusyError:
                return "busy"
            return "won"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ["worker-a", "worker-b"]))

    assert sorted(outcomes) == ["busy", "won"]
    check = connect_database(database_path)
    try:
        row = check.execute(
            "SELECT owner_id, lease_token, state_version FROM leases "
            "WHERE resource_type = 'attempt' AND resource_id = 'attempt-race'"
        ).fetchone()
        assert row["owner_id"] in {"worker-a", "worker-b"}
        assert row["lease_token"] == f"token-{row['owner_id']}"
        assert row["state_version"] == 0
    finally:
        check.close()
