from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from data_generation_agent.feishu.models import (
    AcceptedSourceRecord,
    IngestionSnapshot,
    RejectedSourceRecord,
    source_content_hash,
)
from data_generation_agent.feishu.snapshots import (
    SnapshotConflictError,
    SnapshotStore,
    SnapshotStoreError,
)
from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import initialize_database


def _snapshot(question: str = "How should safety stock be calculated?") -> IngestionSnapshot:
    fields_json = json.dumps({"question": question}, sort_keys=True, separators=(",", ":"))
    content_hash = source_content_hash(
        source_id="development_seeds",
        table_id="tbl-local-only",
        view_id=None,
        base_record_id="rec-1",
        source_record_id="seed-1",
        fields_json=fields_json,
    )
    accepted = AcceptedSourceRecord(
        source_record_id="seed-1",
        base_record_id="rec-1",
        record_revision="rev-1",
        question=question,
        fields_json=fields_json,
        content_hash=content_hash,
    )
    rejected = RejectedSourceRecord(
        source_record_id=None,
        base_record_id="rec-2",
        record_revision="rev-2",
        issue_codes=("EMPTY_QUESTION",),
        raw_content_hash="b" * 64,
    )
    payload = {
        "schema_version": "feishu_ingestion_snapshot.v1",
        "source": {
            "source_id": "development_seeds",
            "registration_digest": "a" * 64,
        },
        "start_offset": 0,
        "end_offset": 2,
        "page_count": 1,
        "source_record_count": 2,
        "accepted": [accepted.to_payload()],
        "rejected": [rejected.to_payload()],
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    snapshot_id = hashlib.sha256(payload_json.encode()).hexdigest()
    return IngestionSnapshot(
        schema_version="feishu_ingestion_snapshot.v1",
        snapshot_id=snapshot_id,
        source_id="development_seeds",
        table_id="tbl-local-only",
        view_id=None,
        start_offset=0,
        end_offset=2,
        page_count=1,
        source_record_count=2,
        accepted=(accepted,),
        rejected=(rejected,),
        payload_json=payload_json,
    )


def _partial(next_offset: int) -> dict[str, object]:
    return {
        "schema_version": "feishu_ingestion_partial.v1",
        "source_id": "development_seeds",
        "registration_digest": "a" * 64,
        "next_offset": next_offset,
    }


def test_snapshot_commit_is_immutable_idempotent_and_tracks_revisions(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        artifacts = ArtifactStore(tmp_path / "artifacts", connection)
        store = SnapshotStore(connection, artifacts)
        first_artifact, first_created = store.commit(_snapshot())
        second_artifact, second_created = store.commit(_snapshot())
        assert first_created is True
        assert second_created is False
        assert first_artifact == second_artifact
        assert connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_record_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_rejections").fetchone()[0] == 1
        cursor = store.cursor("development_seeds")
        assert cursor is not None
        assert cursor.next_offset == 0
        assert cursor.last_snapshot_id == _snapshot().snapshot_id
    finally:
        connection.close()


def test_same_source_revision_with_changed_content_fails_closed(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        store = SnapshotStore(connection, ArtifactStore(tmp_path / "artifacts", connection))
        store.commit(_snapshot("original"))
        with pytest.raises(SnapshotConflictError, match="source revision"):
            store.commit(_snapshot("changed"))
        assert connection.execute("SELECT count(*) FROM source_record_revisions").fetchone()[0] == 1
    finally:
        connection.close()


def test_checkpoint_uses_cas_and_commit_clears_partial_state(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        artifacts = ArtifactStore(tmp_path / "artifacts", connection)
        store = SnapshotStore(connection, artifacts)
        partial = artifacts.put_json(_partial(2), kind="feishu_partial_page")
        cursor = store.checkpoint(
            "development_seeds",
            next_offset=2,
            partial_artifact_id=partial.artifact_id,
            expected_version=None,
        )
        assert cursor.next_offset == 2
        with pytest.raises(SnapshotConflictError, match="version"):
            store.checkpoint(
                "development_seeds",
                next_offset=3,
                partial_artifact_id=partial.artifact_id,
                expected_version=0,
            )
        store.commit(
            _snapshot(),
            expected_cursor_version=cursor.state_version,
            expected_next_offset=cursor.next_offset,
        )
        completed = store.cursor("development_seeds")
        assert completed is not None
        assert completed.next_offset == 0
        assert completed.partial_artifact_id is None
    finally:
        connection.close()


def test_migration_two_is_applied_and_idempotent(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2]
    finally:
        connection.close()
    reopened = initialize_database(tmp_path / "harness.sqlite3")
    try:
        assert reopened.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
    finally:
        reopened.close()


def test_snapshot_identity_and_tables_are_immutable(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        store = SnapshotStore(connection, ArtifactStore(tmp_path / "artifacts", connection))
        snapshot = _snapshot()
        invalid = IngestionSnapshot(
            **{**snapshot.__dict__, "snapshot_id": "0" * 64}
        )
        with pytest.raises(SnapshotConflictError, match="payload SHA-256"):
            store.commit(invalid)
        store.commit(snapshot)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_snapshots SET source_id = 'changed' WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
    finally:
        connection.close()


def test_duplicate_and_missing_record_ids_remain_distinct_rejections(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        artifacts = ArtifactStore(tmp_path / "artifacts", connection)
        store = SnapshotStore(connection, artifacts)
        rejected = (
            RejectedSourceRecord(None, "", None, ("MISSING_RECORD_ID",), "c" * 64),
            RejectedSourceRecord("dup", "rec-dup", "r1", ("DUPLICATE_ID",), "d" * 64),
            RejectedSourceRecord("dup", "rec-dup", "r1", ("DUPLICATE_ID",), "d" * 64),
        )
        payload = {
            "schema_version": "feishu_ingestion_snapshot.v1",
            "source": {
                "source_id": "development_seeds",
                "registration_digest": "a" * 64,
            },
            "start_offset": 0,
            "end_offset": 3,
            "page_count": 1,
            "source_record_count": 3,
            "accepted": [],
            "rejected": [item.to_payload() for item in rejected],
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        snapshot = IngestionSnapshot(
            schema_version="feishu_ingestion_snapshot.v1",
            snapshot_id=hashlib.sha256(payload_json.encode()).hexdigest(),
            source_id="development_seeds",
            table_id="tbl-local-only",
            view_id=None,
            start_offset=0,
            end_offset=3,
            page_count=1,
            source_record_count=3,
            accepted=(),
            rejected=rejected,
            payload_json=payload_json,
        )
        store.commit(snapshot)
        assert connection.execute("SELECT count(*) FROM source_rejections").fetchone()[0] == 3
    finally:
        connection.close()


def test_payload_and_relational_rows_cannot_diverge(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        store = SnapshotStore(connection, ArtifactStore(tmp_path / "artifacts", connection))
        snapshot = _snapshot("artifact-question")
        changed_fields = json.dumps(
            {"question": "relational-question"}, sort_keys=True, separators=(",", ":")
        )
        changed = AcceptedSourceRecord(
            source_record_id="seed-1",
            base_record_id="rec-1",
            record_revision="rev-1",
            question="relational-question",
            fields_json=changed_fields,
            content_hash=source_content_hash(
                source_id="development_seeds",
                table_id="tbl-local-only",
                view_id=None,
                base_record_id="rec-1",
                source_record_id="seed-1",
                fields_json=changed_fields,
            ),
        )
        tampered = IngestionSnapshot(
            **{**snapshot.__dict__, "accepted": (changed,)}
        )
        with pytest.raises(SnapshotConflictError, match="accepted"):
            store.commit(tampered)
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0
    finally:
        connection.close()


def test_stale_worker_cannot_clear_a_newer_checkpoint(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        artifacts = ArtifactStore(tmp_path / "artifacts", connection)
        store = SnapshotStore(connection, artifacts)
        first_page = artifacts.put_json(_partial(1), kind="feishu_partial_page")
        observed = store.checkpoint(
            "development_seeds",
            next_offset=1,
            partial_artifact_id=first_page.artifact_id,
            expected_version=None,
        )
        second_page = artifacts.put_json(_partial(2), kind="feishu_partial_page")
        newer = store.checkpoint(
            "development_seeds",
            next_offset=2,
            partial_artifact_id=second_page.artifact_id,
            expected_version=observed.state_version,
        )
        with pytest.raises(SnapshotConflictError, match="version"):
            store.commit(
                _snapshot(),
                expected_cursor_version=observed.state_version,
                expected_next_offset=observed.next_offset,
            )
        assert store.cursor("development_seeds") == newer
    finally:
        connection.close()


def test_snapshot_store_requires_one_transaction_connection(tmp_path: Path) -> None:
    first = initialize_database(tmp_path / "first.sqlite3")
    second = initialize_database(tmp_path / "second.sqlite3")
    try:
        with pytest.raises(SnapshotStoreError, match="share one database connection"):
            SnapshotStore(first, ArtifactStore(tmp_path / "artifacts", second))
    finally:
        first.close()
        second.close()


def test_forged_content_hash_is_rejected_before_artifact_write(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        artifacts = ArtifactStore(tmp_path / "artifacts", connection)
        store = SnapshotStore(connection, artifacts)
        snapshot = _snapshot()
        original = snapshot.accepted[0]
        forged = AcceptedSourceRecord(
            source_record_id=original.source_record_id,
            base_record_id=original.base_record_id,
            record_revision=original.record_revision,
            question=original.question,
            fields_json=original.fields_json,
            content_hash="0" * 64,
        )
        tampered = IngestionSnapshot(
            **{**snapshot.__dict__, "accepted": (forged,)}
        )
        with pytest.raises(SnapshotConflictError, match="content_hash"):
            store.commit(tampered)
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    finally:
        connection.close()
