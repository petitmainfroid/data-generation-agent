from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.outbox import (
    OutboxConflictError,
    OutboxLeaseError,
    OutboxStore,
    SecretPayloadError,
)


T0 = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _insert_job(connection, job_id: str = "job-1") -> None:
    now = T0.isoformat().replace("+00:00", "Z")
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, job_type, status, policy_digest, config_json, created_at, updated_at
        ) VALUES (?, 'review', 'PENDING', ?, '{}', ?, ?)
        """,
        (job_id, "0" * 64, now, now),
    )


@pytest.fixture
def outbox(tmp_path: Path):
    connection = initialize_database(tmp_path / "harness.sqlite3")
    _insert_job(connection)
    store = OutboxStore(connection)
    try:
        yield store
    finally:
        connection.close()


def _enqueue(store: OutboxStore, *, payload=None, dedupe_key: str = "candidate:q1:r1"):
    return store.enqueue(
        job_id="job-1",
        aggregate_type="candidate",
        aggregate_id="q1",
        destination="feishu:development:candidate-results",
        operation="PATCH",
        dedupe_key=dedupe_key,
        payload=payload or {"fields": {"status": "ACCEPTED", "token_count": 123}},
        available_at=T0,
    )


def test_enqueue_is_deduplicated_and_conflicting_reuse_fails(outbox: OutboxStore) -> None:
    first = _enqueue(outbox)
    second = _enqueue(outbox)

    assert first == second
    assert first.status == "PENDING"
    assert first.delivery_key == "candidate:q1:r1"
    assert outbox.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1

    with pytest.raises(OutboxConflictError, match="different write"):
        _enqueue(outbox, payload={"fields": {"status": "REJECTED"}})
    assert outbox.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "not-allowed"},
        {"nested": {"access_token": "not-allowed"}},
        {"header": "Authorization: Bearer abc.def.ghi"},
        {"note": "sk-abcdefghijklmnopqrstuvwxyz"},
    ],
)
def test_payload_rejects_secret_keys_and_values(
    outbox: OutboxStore, payload: dict[str, object]
) -> None:
    with pytest.raises(SecretPayloadError):
        _enqueue(outbox, payload=payload, dedupe_key=f"secret:{len(str(payload))}")
    assert outbox.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_claim_is_exclusive_and_ack_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "harness.sqlite3"
    first_connection = initialize_database(database)
    _insert_job(first_connection)
    second_connection = initialize_database(database)
    first_store = OutboxStore(first_connection)
    second_store = OutboxStore(second_connection)
    try:
        queued = _enqueue(first_store)
        claimed = first_store.claim("worker-a", now=T0, lease_seconds=30)
        assert [record.outbox_id for record in claimed] == [queued.outbox_id]
        assert claimed[0].attempt_count == 1
        assert second_store.claim("worker-b", now=T0, lease_seconds=30) == []

        with pytest.raises(OutboxLeaseError):
            first_store.ack(queued.outbox_id, "wrong-token", now=T0)
        sent = first_store.ack(
            queued.outbox_id,
            claimed[0].lease_token or "",
            response={"record_id": "rec-1"},
            now=T0,
        )
        assert sent.status == "SENT"
        assert sent.sent_at is not None
        assert sent.response == {"record_id": "rec-1"}
        assert sent.lease_token is None

        repeated_ack = second_store.ack(queued.outbox_id, "lost-local-token", now=T0)
        assert repeated_ack == sent
        assert second_store.claim("worker-b", now=T0 + timedelta(hours=1)) == []
    finally:
        second_connection.close()
        first_connection.close()


def test_retry_respects_availability_and_redacts_errors(outbox: OutboxStore) -> None:
    queued = _enqueue(outbox)
    first = outbox.claim("worker-a", now=T0, lease_seconds=30)[0]
    retried = outbox.retry(
        queued.outbox_id,
        first.lease_token or "",
        "HTTP failed Authorization: Bearer abc.def.ghi api_key=plain-secret",
        delay_seconds=10,
        now=T0,
    )

    assert retried.status == "RETRY_WAIT"
    assert retried.attempt_count == 1
    assert retried.lease_token is None
    assert "abc.def.ghi" not in (retried.last_error or "")
    assert "plain-secret" not in (retried.last_error or "")
    assert "[REDACTED]" in (retried.last_error or "")
    assert outbox.claim("worker-b", now=T0 + timedelta(seconds=9)) == []
    second = outbox.claim("worker-b", now=T0 + timedelta(seconds=10))[0]
    assert second.outbox_id == queued.outbox_id
    assert second.attempt_count == 2


def test_lost_ack_reclaims_same_payload_without_rerunning_model(outbox: OutboxStore) -> None:
    model_invocations = 0

    def completed_model_result() -> dict[str, object]:
        nonlocal model_invocations
        model_invocations += 1
        return {"fields": {"decision": "ACCEPT", "result_id": "result-1"}}

    queued = _enqueue(outbox, payload=completed_model_result(), dedupe_key="write:result-1")
    first = outbox.claim("worker-a", now=T0, lease_seconds=10)[0]
    # The remote write succeeds, but the worker dies before committing ack().
    reclaimed = outbox.claim("worker-b", now=T0 + timedelta(seconds=11), lease_seconds=10)[0]

    assert reclaimed.outbox_id == first.outbox_id == queued.outbox_id
    assert reclaimed.dedupe_key == first.dedupe_key
    assert reclaimed.payload_hash == first.payload_hash
    assert reclaimed.payload == first.payload
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_token != first.lease_token
    assert model_invocations == 1

    sent = outbox.ack(
        reclaimed.outbox_id,
        reclaimed.lease_token or "",
        response={"readback_matches": True},
        now=T0 + timedelta(seconds=12),
    )
    assert sent.status == "SENT"
    reused = outbox.enqueue(
        job_id="job-1",
        aggregate_type="candidate",
        aggregate_id="q1",
        destination="feishu:development:candidate-results",
        operation="PATCH",
        dedupe_key="write:result-1",
        payload=first.payload,
        available_at=T0,
    )
    assert reused.status == "SENT"
    assert reused.outbox_id == sent.outbox_id
    assert model_invocations == 1


def test_retry_can_fail_terminally_at_attempt_limit(outbox: OutboxStore) -> None:
    queued = _enqueue(outbox)
    claimed = outbox.claim("worker-a", now=T0)[0]
    failed = outbox.retry(
        queued.outbox_id,
        claimed.lease_token or "",
        "permanent failure",
        max_attempts=1,
        now=T0,
    )
    assert failed.status == "FAILED"
    assert failed.sent_at is None
    assert outbox.claim("worker-b", now=T0 + timedelta(days=1)) == []
