from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from data_generation_agent.feishu.dispatcher import (
    FeishuOutboxDispatcher,
    FeishuWriteResult,
    FeishuWriter,
)
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.outbox import OutboxLeaseError, OutboxStore


T0 = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _insert_job(connection) -> None:
    now = T0.isoformat().replace("+00:00", "Z")
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, job_type, status, policy_digest, config_json, created_at, updated_at
        ) VALUES ('job-1', 'review', 'PENDING', ?, '{}', ?, ?)
        """,
        ("0" * 64, now, now),
    )


@pytest.fixture
def outbox(tmp_path: Path):
    connection = initialize_database(tmp_path / "dispatcher.sqlite3")
    _insert_job(connection)
    store = OutboxStore(connection)
    try:
        yield store
    finally:
        connection.close()


def _enqueue(
    store: OutboxStore,
    *,
    destination: str = "feishu:development:candidates",
    payload: dict[str, Any] | None = None,
    operation: str = "PATCH",
    dedupe_key: str = "candidate:q1:r1",
):
    return store.enqueue(
        job_id="job-1",
        aggregate_type="candidate",
        aggregate_id="q1",
        destination=destination,
        operation=operation,
        dedupe_key=dedupe_key,
        payload=(
            {"record_id": "rec-1", "fields": {"status": "ACCEPTED"}}
            if payload is None
            else payload
        ),
        available_at=T0,
    )


class FakeWriter:
    def __init__(self, *scripted: FeishuWriteResult | Exception) -> None:
        self.scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> FeishuWriteResult:
        self.calls.append(dict(kwargs))
        value = self.scripted.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_verified_write_is_acked_with_aliases_and_frozen_delivery_key(
    outbox: OutboxStore,
) -> None:
    queued = _enqueue(outbox)
    writer = FakeWriter(
        FeishuWriteResult(
            success=True,
            readback_verified=True,
            response={"record_id": "rec-1"},
        )
    )
    dispatcher = FeishuOutboxDispatcher(outbox, writer)

    delivered = dispatcher.run_once("worker-a", now=T0, lease_seconds=30)

    assert isinstance(writer, FeishuWriter)
    assert len(delivered) == 1
    assert delivered[0].outbox_id == queued.outbox_id
    assert delivered[0].status == "SENT"
    assert delivered[0].response == {
        "readback_verified": True,
        "record_id": "rec-1",
    }
    assert writer.calls == [
        {
            "base_alias": "development",
            "table_alias": "candidates",
            "operation": "PATCH",
            "record_id": "rec-1",
            "fields": writer.calls[0]["fields"],
            "delivery_key": "candidate:q1:r1",
        }
    ]
    assert dict(writer.calls[0]["fields"]) == {"status": "ACCEPTED"}
    writer.calls[0]["fields"]["status"] = "MUTATED"
    assert outbox.get(queued.outbox_id).payload == queued.payload


@pytest.mark.parametrize(
    ("destination", "payload"),
    [
        ("http:development:candidates", {"fields": {"status": "OK"}}),
        ("feishu:development:candidates:extra", {"fields": {"status": "OK"}}),
        ("feishu:Development:candidates", {"fields": {"status": "OK"}}),
        ("feishu:development:candidates", {"fields": {}}),
        ("feishu:development:candidates", {"fields": ["not-an-object"]}),
        ("feishu:development:candidates", {"record_id": "", "fields": {"status": "OK"}}),
        ("feishu:development:candidates", {"fields": {"Status": "OK"}}),
        ("feishu:development:candidates", {"fields": {"status": "OK"}, "extra": True}),
    ],
)
def test_invalid_destination_or_payload_retries_without_calling_writer(
    outbox: OutboxStore,
    destination: str,
    payload: dict[str, Any],
) -> None:
    queued = _enqueue(outbox, destination=destination, payload=payload)
    writer = FakeWriter()
    result = FeishuOutboxDispatcher(outbox, writer, max_attempts=2).run_once(
        "worker-a", now=T0
    )[0]

    assert result.outbox_id == queued.outbox_id
    assert result.status == "RETRY_WAIT"
    assert result.attempt_count == 1
    assert result.last_error
    assert writer.calls == []


def test_payload_hash_mismatch_is_retried_without_remote_write(outbox: OutboxStore) -> None:
    queued = _enqueue(outbox)
    outbox.connection.execute(
        "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
        (json.dumps({"fields": {"status": "TAMPERED"}}), queued.outbox_id),
    )
    writer = FakeWriter()

    result = FeishuOutboxDispatcher(outbox, writer).run_once(
        "worker-a", now=T0
    )[0]

    assert result.status == "RETRY_WAIT"
    assert "payload hash" in (result.last_error or "")
    assert writer.calls == []


def test_success_without_verified_readback_is_not_acked(outbox: OutboxStore) -> None:
    queued = _enqueue(outbox)
    writer = FakeWriter(
        FeishuWriteResult(success=True, readback_verified=False, response={"ok": True})
    )

    result = FeishuOutboxDispatcher(outbox, writer).run_once(
        "worker-a", now=T0
    )[0]

    assert result.outbox_id == queued.outbox_id
    assert result.status == "RETRY_WAIT"
    assert result.sent_at is None
    assert "READBACK_NOT_VERIFIED" in (result.last_error or "")


def test_writer_errors_are_redacted_and_stop_at_max_attempts(outbox: OutboxStore) -> None:
    queued = _enqueue(outbox)
    secret_error = RuntimeError(
        "HTTP Authorization: Bearer abc.def.ghi api_key=plain-secret"
    )
    writer = FakeWriter(secret_error, secret_error)
    dispatcher = FeishuOutboxDispatcher(outbox, writer, max_attempts=2)

    first = dispatcher.run_once("worker-a", now=T0)[0]
    second = dispatcher.run_once("worker-b", now=T0)[0]

    assert first.status == "RETRY_WAIT"
    assert second.outbox_id == queued.outbox_id
    assert second.status == "FAILED"
    assert second.attempt_count == 2
    assert "abc.def.ghi" not in (second.last_error or "")
    assert "plain-secret" not in (second.last_error or "")
    assert "[REDACTED]" in (second.last_error or "")
    assert outbox.claim("worker-c", now=T0 + timedelta(days=1)) == []
    assert len(writer.calls) == 2


class LostAckStore(OutboxStore):
    def __init__(self, connection) -> None:
        super().__init__(connection)
        self.fail_next_ack = True

    def ack(self, *args: Any, **kwargs: Any):
        if self.fail_next_ack:
            self.fail_next_ack = False
            raise RuntimeError("simulated worker crash before local ACK commit")
        return super().ack(*args, **kwargs)


def test_lost_ack_reclaims_same_dedupe_without_recomputing_payload(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "lost-ack.sqlite3")
    _insert_job(connection)
    store = LostAckStore(connection)
    model_calls = 0

    def completed_model_payload() -> dict[str, Any]:
        nonlocal model_calls
        model_calls += 1
        return {"fields": {"decision": "ACCEPT", "result_id": "result-1"}}

    try:
        queued = _enqueue(
            store,
            payload=completed_model_payload(),
            operation="UPSERT",
            dedupe_key="write:result-1",
        )
        writer = FakeWriter(
            FeishuWriteResult(True, True, {"record_id": "rec-1"}),
            FeishuWriteResult(True, True, {"record_id": "rec-1"}),
        )
        dispatcher = FeishuOutboxDispatcher(store, writer)

        with pytest.raises(RuntimeError, match="before local ACK"):
            dispatcher.run_once("worker-a", now=T0, lease_seconds=10)
        in_flight = store.get(queued.outbox_id)
        assert in_flight.status == "IN_FLIGHT"

        sent = dispatcher.run_once(
            "worker-b", now=T0 + timedelta(seconds=11), lease_seconds=10
        )[0]
        assert sent.status == "SENT"
        assert [call["delivery_key"] for call in writer.calls] == [
            "write:result-1",
            "write:result-1",
        ]
        assert writer.calls[0]["fields"] == writer.calls[1]["fields"]
        assert model_calls == 1
    finally:
        connection.close()


def test_reclaimed_message_rejects_old_lease_before_writer(outbox: OutboxStore) -> None:
    _enqueue(outbox)
    stale = outbox.claim("worker-a", now=T0, lease_seconds=10)[0]
    current = outbox.claim(
        "worker-b", now=T0 + timedelta(seconds=11), lease_seconds=10
    )[0]
    writer = FakeWriter(FeishuWriteResult(True, True))
    dispatcher = FeishuOutboxDispatcher(outbox, writer)

    with pytest.raises(OutboxLeaseError, match="stale"):
        dispatcher.dispatch_claimed(stale, now=T0 + timedelta(seconds=11))
    assert writer.calls == []

    sent = dispatcher.dispatch_claimed(current, now=T0 + timedelta(seconds=11))
    assert sent.status == "SENT"
    assert len(writer.calls) == 1
