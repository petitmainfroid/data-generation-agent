from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_generation_agent.harness.artifacts import ArtifactStore, ArtifactStoreError
from data_generation_agent.harness.db import connect_database, initialize_database
from data_generation_agent.harness.leases import LeaseManager, StaleLeaseTokenError
from data_generation_agent.harness.outbox import OutboxLeaseError, OutboxStore
from data_generation_agent.harness.service import HarnessService
from data_generation_agent.harness.store import HarnessStore


T0 = datetime(2026, 7, 11, 16, 0, tzinfo=timezone.utc)
T0_TEXT = T0.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _insert_job(connection: sqlite3.Connection, job_id: str = "job-recovery") -> None:
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, job_type, status, policy_digest, config_json, created_at, updated_at
        ) VALUES (?, 'RECOVERY_TEST', 'PENDING', ?, '{}', ?, ?)
        """,
        (job_id, "a" * 64, T0_TEXT, T0_TEXT),
    )


def test_same_job_can_restart_ten_times_without_duplicate_creation(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    created_flags: list[bool] = []
    records = []

    for _ in range(10):
        connection = initialize_database(database)
        try:
            service = HarnessService(connection, tmp_path / "artifacts")
            record, created = service.create_job(
                "stable-job",
                job_type="AUDIT",
                config={"seed_snapshot": "snapshot-1", "policy": "policy-1"},
            )
            created_flags.append(created)
            records.append(record)
        finally:
            connection.close()

    assert created_flags == [True] + [False] * 9
    assert all(record == records[0] for record in records)
    check = initialize_database(database)
    try:
        assert check.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        events = check.execute(
            "SELECT event_type, event_key FROM events WHERE aggregate_id = 'stable-job'"
        ).fetchall()
        assert [tuple(row) for row in events] == [
            ("JOB_CREATED", "job:stable-job:created")
        ]
    finally:
        check.close()


def test_state_update_rolls_back_when_event_append_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = initialize_database(tmp_path / "atomic.sqlite3")
    try:
        _insert_job(connection)
        store = HarnessStore(connection, clock=lambda: T0)

        def crash_after_state_update(**_: object) -> int:
            raise RuntimeError("injected event append crash")

        monkeypatch.setattr(store, "_append_event", crash_after_state_update)
        with pytest.raises(RuntimeError, match="injected event append crash"):
            store.transition(
                "job",
                "job-recovery",
                "PENDING",
                "RUNNING",
                expected_version=0,
                event_type="JOB_STARTED",
                event_key="job-recovery-started",
            )

        row = connection.execute(
            "SELECT status, state_version FROM jobs WHERE job_id = 'job-recovery'"
        ).fetchone()
        assert dict(row) == {"status": "PENDING", "state_version": 0}
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        connection.close()


def test_confirmed_logical_result_is_not_recomputed_after_restarts(tmp_path: Path) -> None:
    database = tmp_path / "logical-result.sqlite3"
    artifact_root = tmp_path / "artifacts"
    model_calls = 0

    def run_or_resume() -> str:
        nonlocal model_calls
        connection = initialize_database(database)
        try:
            service = HarnessService(connection, artifact_root)
            service.create_job("logical-job", job_type="AUDIT", config={"input": "v1"})
            events = service.store.list_events("job", "logical-job")
            confirmed = next(
                (event for event in events if event.event_type == "LOGICAL_RESULT_CONFIRMED"),
                None,
            )
            if confirmed is not None:
                return str(confirmed.payload["artifact_id"])

            model_calls += 1
            result = {"decision": "ACCEPT", "model_call": model_calls}
            artifact = service.artifacts.put_json(result, kind="logical_result", job_id="logical-job")
            service.store.append_event(
                "job",
                "logical-job",
                "LOGICAL_RESULT_CONFIRMED",
                event_key="logical-job:result:v1",
                job_id="logical-job",
                payload={"artifact_id": artifact.artifact_id},
            )
            return artifact.artifact_id
        finally:
            connection.close()

    artifact_ids = [run_or_resume() for _ in range(10)]
    assert len(set(artifact_ids)) == 1
    assert model_calls == 1

    check = initialize_database(database)
    try:
        assert check.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert (
            check.execute(
                "SELECT count(*) FROM events WHERE event_type = 'LOGICAL_RESULT_CONFIRMED'"
            ).fetchone()[0]
            == 1
        )
    finally:
        check.close()


def test_sqlite_busy_failure_is_clean_and_retryable_after_lock_release(tmp_path: Path) -> None:
    database = tmp_path / "busy.sqlite3"
    setup = initialize_database(database)
    setup.close()
    blocker = connect_database(database)
    contender = connect_database(database, busy_timeout_ms=25)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO jobs "
            "(job_id, job_type, status, policy_digest, config_json, created_at, updated_at) "
            "VALUES ('lock-holder', 'TEST', 'PENDING', ?, '{}', ?, ?)",
            ("b" * 64, T0_TEXT, T0_TEXT),
        )
        service = HarnessService(contender, tmp_path / "artifacts")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            service.create_job("waiter", job_type="AUDIT", config={})
        assert contender.in_transaction is False

        blocker.rollback()
        recovered, created = service.create_job("waiter", job_type="AUDIT", config={})
        assert created is True
        assert recovered.status == "PENDING"
        assert contender.execute(
            "SELECT count(*) FROM events WHERE aggregate_id = 'waiter'"
        ).fetchone()[0] == 1
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        contender.close()
        blocker.close()


def test_artifact_file_is_reconciled_after_database_commit_failure(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "artifact-crash.sqlite3")
    try:
        store = ArtifactStore(tmp_path / "artifacts", connection)
        payload = b"model result durable before database transaction"
        digest = hashlib.sha256(payload).hexdigest()
        path = store.resolve_uri(store.uri_for(digest))

        # The missing Job causes the DB insert to fail after the atomic file rename.
        with pytest.raises(ArtifactStoreError, match="failed to register"):
            store.put_bytes(payload, kind="model_result", job_id="missing-job")
        assert path.read_bytes() == payload
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0

        report = store.reconcile()
        artifact_id = f"sha256:{digest}"
        assert report.inserted_orphans == (artifact_id,)
        assert report.ok is True
        assert store.read_bytes(artifact_id) == payload
    finally:
        connection.close()


def test_outbox_lost_ack_reclaims_same_delivery_key_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "outbox-recovery.sqlite3"
    first_connection = initialize_database(database)
    _insert_job(first_connection)
    first_store = OutboxStore(first_connection)
    queued = first_store.enqueue(
        job_id="job-recovery",
        aggregate_type="candidate",
        aggregate_id="candidate-1",
        destination="feishu:test:candidate-results",
        operation="PATCH",
        dedupe_key="write:candidate-1:revision-1",
        payload={"fields": {"decision": "ACCEPT", "result_id": "result-1"}},
        available_at=T0,
    )
    first_delivery = first_store.claim("worker-a", now=T0, lease_seconds=10)[0]
    first_connection.close()

    # The remote request succeeded, but no local ack was committed before restart.
    remote_delivery_keys = [first_delivery.delivery_key]
    second_connection = initialize_database(database)
    try:
        second_store = OutboxStore(second_connection)
        reclaimed = second_store.claim(
            "worker-b", now=T0 + timedelta(seconds=11), lease_seconds=10
        )[0]
        remote_delivery_keys.append(reclaimed.delivery_key)

        assert reclaimed.outbox_id == queued.outbox_id
        assert reclaimed.payload_hash == first_delivery.payload_hash
        assert reclaimed.payload == first_delivery.payload
        assert reclaimed.attempt_count == 2
        assert reclaimed.lease_token != first_delivery.lease_token
        assert remote_delivery_keys == [queued.dedupe_key, queued.dedupe_key]

        with pytest.raises(OutboxLeaseError):
            second_store.ack(
                queued.outbox_id,
                first_delivery.lease_token or "",
                now=T0 + timedelta(seconds=12),
            )
        sent = second_store.ack(
            queued.outbox_id,
            reclaimed.lease_token or "",
            response={"readback_matches": True},
            now=T0 + timedelta(seconds=12),
        )
        assert sent.status == "SENT"
        assert sent.delivery_key == queued.dedupe_key
    finally:
        second_connection.close()


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_expired_lease_token_is_fenced_after_new_owner_takes_over(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "lease-fencing.sqlite3")
    clock = _MutableClock(T0)
    tokens = iter(["lease-old", "lease-new"])
    manager = LeaseManager(connection, clock=clock, token_factory=lambda: next(tokens))
    try:
        old = manager.acquire("candidate", "candidate-1", "worker-a", ttl_seconds=10)
        clock.value = T0 + timedelta(seconds=11)
        new = manager.acquire("candidate", "candidate-1", "worker-b", ttl_seconds=10)

        assert old.lease_token == "lease-old"
        assert new.lease_token == "lease-new"
        assert new.state_version == old.state_version + 1
        assert manager.assert_valid("candidate", "candidate-1", new.lease_token) == new
        with pytest.raises(StaleLeaseTokenError):
            manager.assert_valid("candidate", "candidate-1", old.lease_token)
        with pytest.raises(StaleLeaseTokenError):
            manager.renew("candidate", "candidate-1", old.lease_token, ttl_seconds=10)
        with pytest.raises(StaleLeaseTokenError):
            manager.release("candidate", "candidate-1", old.lease_token)
    finally:
        connection.close()
