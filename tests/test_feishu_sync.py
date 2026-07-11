from __future__ import annotations

from pathlib import Path

from data_generation_agent.feishu.sync import (
    enqueue_feishu_write,
    rebuild_and_enqueue_progress,
)
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.outbox import OutboxStore
from data_generation_agent.harness.service import HarnessService


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "harness.sqlite3"
    connection = initialize_database(path)
    try:
        HarnessService(connection, tmp_path / "artifacts").create_job(
            "job-1", job_type="TEST"
        )
    finally:
        connection.close()
    return path


def test_enqueue_feishu_write_freezes_only_logical_aliases(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = initialize_database(path)
    try:
        record = enqueue_feishu_write(
            OutboxStore(connection),
            job_id="job-1",
            base_alias="development",
            table_alias="seeds",
            aggregate_type="seed",
            aggregate_id="seed-1",
            dedupe_key="seed-write-1",
            fields={"sft_id": "seed-1", "question": "Question"},
        )
        assert record.destination == "feishu:development:seeds"
        assert record.payload == {
            "fields": {"sft_id": "seed-1", "question": "Question"}
        }
        assert "fld" not in repr(record.payload)
    finally:
        connection.close()


def test_counter_rebuild_enqueues_one_idempotent_progress_write(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = initialize_database(path)
    try:
        for candidate, status in (
            ("candidate-1", "pending"),
            ("candidate-2", "passed"),
            ("candidate-3", "rejected"),
        ):
            connection.execute(
                "INSERT INTO events(event_key, job_id, aggregate_type, aggregate_id, "
                "event_type, payload_json, created_at) VALUES (?, 'job-1', 'candidate', ?, "
                "'CANDIDATE_STAGE_STATUS', ?, '2026-07-11T00:00:00Z')",
                (
                    f"event:{candidate}",
                    candidate,
                    '{"batch_id":"batch-1","task_mode":"GENERATE",'
                    '"question_type":"smoke","stage":"quality_review",'
                    f'"status":"{status}"}}',
                ),
            )
    finally:
        connection.close()

    first = rebuild_and_enqueue_progress(
        database_path=path,
        job_id="job-1",
        base_alias="development",
        machine_target=5,
        qualified_target=2,
    )
    second = rebuild_and_enqueue_progress(
        database_path=path,
        job_id="job-1",
        base_alias="development",
        machine_target=5,
        qualified_target=2,
    )
    assert first == second
    assert first["counter_count"] == 1
    check = initialize_database(path)
    try:
        row = check.execute("SELECT payload_json FROM outbox").fetchone()
        assert '"pending":1' in row["payload_json"]
        assert '"passed":1' in row["payload_json"]
        assert '"rejected":1' in row["payload_json"]
        assert '"machine_remaining":2' in row["payload_json"]
        assert '"qualified_deficit":1' in row["payload_json"]
        assert check.execute("SELECT count(1) FROM outbox").fetchone()[0] == 1
    finally:
        check.close()
