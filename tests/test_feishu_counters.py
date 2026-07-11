from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_generation_agent.feishu.counters import (
    CounterPolicy,
    InvalidCounterEventError,
    UnknownStageStatusError,
    progress_outbox_descriptor,
    rebuild_stage_counters,
)
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.outbox import OutboxStore
from data_generation_agent.harness.store import EventRecord


def _event(
    event_id: int,
    candidate_id: str,
    status: str,
    *,
    batch_id: str = "batch-1",
    task_mode: str = "generate",
    question_type: str = "reasoning",
    stage: str = "quality_review",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": "CANDIDATE_STAGE_STATUS",
        "aggregate_type": "candidate",
        "aggregate_id": candidate_id,
        "payload": {
            "batch_id": batch_id,
            "task_mode": task_mode,
            "question_type": question_type,
            "stage": stage,
            "status": status,
        },
    }


def test_latest_event_wins_despite_duplicates_and_input_order() -> None:
    events = [
        _event(7, "candidate-pending", "PENDING"),
        _event(2, "candidate-running", "running"),
        _event(1, "candidate-passed", "pending"),
        _event(6, "candidate-quarantined", "quarantined"),
        _event(5, "candidate-rejected", "rejected"),
        _event(4, "candidate-passed", "passed"),
        _event(4, "candidate-passed", "passed"),
    ]

    projection = rebuild_stage_counters(
        events,
        policy=CounterPolicy(machine_target=8, qualified_target=3),
    )

    assert len(projection) == 1
    row = projection[0]
    assert (
        row.pending,
        row.running,
        row.passed,
        row.rejected,
        row.quarantined,
    ) == (1, 1, 1, 1, 1)
    assert row.candidate_count == 5
    assert row.machine_remaining == 3
    assert row.qualified_deficit == 2
    assert row.through_event_id == 7


def test_rebuild_is_deterministic_and_groups_all_dimensions() -> None:
    events = [
        _event(3, "candidate-3", "passed", question_type="math"),
        _event(1, "candidate-1", "pending"),
        _event(2, "candidate-2", "running", stage="difficulty_prescreen"),
    ]
    policy = CounterPolicy(machine_target=4, qualified_target=2)

    forward = rebuild_stage_counters(events, policy=policy)
    reverse = rebuild_stage_counters(reversed(events), policy=policy)

    assert forward == reverse
    assert [row.dimensions.question_type for row in forward] == ["math", "reasoning", "reasoning"]
    assert [row.dimensions.stage for row in forward] == [
        "quality_review",
        "difficulty_prescreen",
        "quality_review",
    ]
    assert len({row.stats_key for row in forward}) == 3


def test_event_record_and_payload_json_rows_are_supported() -> None:
    payload = _event(1, "candidate-1", "passed")["payload"]
    event_record = EventRecord(
        event_id=1,
        event_key="event-1",
        job_id="job-1",
        aggregate_type="candidate",
        aggregate_id="candidate-1",
        event_type="CANDIDATE_STAGE_STATUS",
        payload=dict(payload),
        causation_id=None,
        correlation_id=None,
        created_at="2026-07-11T00:00:00Z",
    )
    row = {
        "event_id": 2,
        "aggregate_type": "candidate",
        "aggregate_id": "candidate-2",
        "event_type": "CANDIDATE_STAGE_STATUS",
        "payload_json": json.dumps(_event(2, "candidate-2", "pending")["payload"]),
    }

    projection = rebuild_stage_counters(
        [event_record, row],
        policy=CounterPolicy(machine_target=2, qualified_target=1),
    )

    assert len(projection) == 1
    assert projection[0].passed == 1
    assert projection[0].pending == 1
    assert projection[0].machine_remaining == 0
    assert projection[0].qualified_deficit == 0


def test_unrelated_events_are_ignored_but_relevant_events_fail_closed() -> None:
    unrelated = {"event_type": "JOB_STARTED"}
    assert rebuild_stage_counters(
        [unrelated], policy=CounterPolicy(machine_target=0, qualified_target=0)
    ) == ()

    unknown = _event(1, "candidate-1", "failed")
    with pytest.raises(UnknownStageStatusError, match="unknown"):
        rebuild_stage_counters(
            [unknown], policy=CounterPolicy(machine_target=1, qualified_target=1)
        )

    missing = _event(2, "candidate-2", "pending")
    del missing["payload"]["batch_id"]  # type: ignore[index]
    with pytest.raises(InvalidCounterEventError, match="batch_id"):
        rebuild_stage_counters(
            [missing], policy=CounterPolicy(machine_target=1, qualified_target=1)
        )


def test_duplicate_event_id_with_different_content_fails_closed() -> None:
    with pytest.raises(InvalidCounterEventError, match="conflicting"):
        rebuild_stage_counters(
            [_event(1, "candidate-1", "pending"), _event(1, "candidate-1", "passed")],
            policy=CounterPolicy(machine_target=1, qualified_target=1),
        )


def test_progress_descriptor_uses_only_aliases_and_stable_content_dedupe() -> None:
    policy = CounterPolicy(machine_target=3, qualified_target=2)
    first_counter = rebuild_stage_counters(
        [_event(2, "candidate-2", "passed"), _event(1, "candidate-1", "pending")],
        policy=policy,
    )[0]
    rebuilt_counter = rebuild_stage_counters(
        [_event(1, "candidate-1", "pending"), _event(2, "candidate-2", "passed")],
        policy=policy,
    )[0]

    first = progress_outbox_descriptor(first_counter, base_alias="development")
    rebuilt = progress_outbox_descriptor(rebuilt_counter, base_alias="development")

    assert first == rebuilt
    assert first.aggregate_type == "progress_stats"
    assert first.aggregate_id == first_counter.stats_key
    assert first.destination == "feishu:development:progress"
    assert first.operation == "UPSERT"
    assert first.dedupe_key.startswith("feishu-progress:")
    assert set(first.payload) == {"fields"}
    assert set(first.payload["fields"]) == {
        "stats_key",
        "batch_id",
        "task_mode",
        "question_type",
        "stage",
        "pending",
        "running",
        "passed",
        "rejected",
        "quarantined",
        "machine_remaining",
        "qualified_deficit",
    }
    serialized = json.dumps(first.payload, sort_keys=True)
    for forbidden in ("table_id", "field_id", "base_token", "access_token"):
        assert forbidden not in serialized

    changed = progress_outbox_descriptor(
        replace(first_counter, machine_remaining=0), base_alias="development"
    )
    other_destination = progress_outbox_descriptor(first_counter, base_alias="staging")
    assert changed.aggregate_id == first.aggregate_id
    assert changed.dedupe_key != first.dedupe_key
    assert other_destination.dedupe_key != first.dedupe_key


def test_progress_descriptor_is_directly_deduplicated_by_outbox(tmp_path: Path) -> None:
    counter = rebuild_stage_counters(
        [_event(1, "candidate-1", "pending")],
        policy=CounterPolicy(machine_target=2, qualified_target=1),
    )[0]
    descriptor = progress_outbox_descriptor(counter, base_alias="development")
    connection = initialize_database(tmp_path / "harness.sqlite3")
    now = "2026-07-11T00:00:00.000000Z"
    try:
        connection.execute(
            "INSERT INTO jobs(job_id, job_type, status, policy_digest, config_json, "
            "created_at, updated_at) VALUES ('job-1', 'counter', 'PENDING', ?, '{}', ?, ?)",
            ("a" * 64, now, now),
        )
        store = OutboxStore(connection)

        def enqueue():
            return store.enqueue(
                job_id="job-1",
                aggregate_type=descriptor.aggregate_type,
                aggregate_id=descriptor.aggregate_id,
                destination=descriptor.destination,
                operation=descriptor.operation,
                dedupe_key=descriptor.dedupe_key,
                payload=descriptor.payload,
                available_at=now,
            )

        first = enqueue()
        repeated = enqueue()
        assert repeated == first
        assert first.payload == descriptor.payload
        assert first.delivery_key == descriptor.dedupe_key
        assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"machine_target": -1, "qualified_target": 0}, ValueError),
        ({"machine_target": True, "qualified_target": 0}, TypeError),
    ],
)
def test_policy_is_explicit_and_validated(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        CounterPolicy(**kwargs)  # type: ignore[arg-type]
