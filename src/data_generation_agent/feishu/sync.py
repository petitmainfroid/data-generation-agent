from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.outbox import OutboxRecord, OutboxStore

from .counters import CounterPolicy, progress_outbox_descriptor, rebuild_stage_counters
from .dispatcher import FeishuOutboxDispatcher
from .profile import BaseProfile
from .writer import LarkCliBaseWriter, LarkCliDispatchWriter


def enqueue_feishu_write(
    store: OutboxStore,
    *,
    job_id: str,
    base_alias: str,
    table_alias: str,
    aggregate_type: str,
    aggregate_id: str,
    dedupe_key: str,
    fields: Mapping[str, Any],
    record_id: str | None = None,
    operation: str = "UPSERT",
) -> OutboxRecord:
    payload: dict[str, Any] = {"fields": dict(fields)}
    if record_id is not None:
        payload["record_id"] = record_id
    return store.enqueue(
        job_id=job_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        destination=f"feishu:{base_alias}:{table_alias}",
        operation=operation,
        dedupe_key=dedupe_key,
        payload=payload,
    )


def dispatch_feishu_outbox(
    *,
    profile_path: Path,
    database_path: Path,
    worker_id: str,
    limit: int = 20,
    lease_seconds: float = 60,
    max_attempts: int = 3,
) -> dict[str, Any]:
    profile = BaseProfile.load(profile_path)
    connection = initialize_database(database_path)
    try:
        outbox = OutboxStore(connection)
        writer = LarkCliDispatchWriter(LarkCliBaseWriter(profile))
        dispatcher = FeishuOutboxDispatcher(
            outbox,
            writer,
            max_attempts=max_attempts,
            retry_delay_seconds=1,
            max_retry_delay_seconds=30,
        )
        records = dispatcher.run_once(
            worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        counts: dict[str, int] = {}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        return {
            "ok": all(record.status == "SENT" for record in records),
            "processed": len(records),
            "status_counts": counts,
            "outbox_ids": [record.outbox_id for record in records],
        }
    finally:
        connection.close()


def rebuild_and_enqueue_progress(
    *,
    database_path: Path,
    job_id: str,
    base_alias: str,
    machine_target: int,
    qualified_target: int,
) -> dict[str, Any]:
    connection = initialize_database(database_path)
    try:
        rows = connection.execute(
            "SELECT event_id, aggregate_type, aggregate_id, event_type, payload_json "
            "FROM events WHERE event_type = 'CANDIDATE_STAGE_STATUS' ORDER BY event_id"
        ).fetchall()
        counters = rebuild_stage_counters(
            rows,
            policy=CounterPolicy(
                machine_target=machine_target,
                qualified_target=qualified_target,
            ),
        )
        outbox = OutboxStore(connection)
        queued: list[OutboxRecord] = []
        for counter in counters:
            descriptor = progress_outbox_descriptor(counter, base_alias=base_alias)
            queued.append(
                outbox.enqueue(
                    job_id=job_id,
                    aggregate_type=descriptor.aggregate_type,
                    aggregate_id=descriptor.aggregate_id,
                    destination=descriptor.destination,
                    operation=descriptor.operation,
                    dedupe_key=descriptor.dedupe_key,
                    payload=descriptor.payload,
                )
            )
        return {
            "ok": True,
            "counter_count": len(counters),
            "queued_count": len(queued),
            "stats_keys": [counter.stats_key for counter in counters],
        }
    finally:
        connection.close()
