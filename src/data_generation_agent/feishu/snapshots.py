from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import transaction

from .models import IngestionSnapshot, source_content_hash


class SnapshotStoreError(RuntimeError):
    """A Feishu snapshot or cursor could not be committed safely."""


class SnapshotConflictError(SnapshotStoreError):
    """A stable snapshot/cursor identity was reused for different content."""


@dataclass(frozen=True)
class IngestionCursor:
    source_id: str
    next_offset: int
    partial_artifact_id: str | None
    last_snapshot_id: str | None
    state_version: int
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class SnapshotStore:
    """Durably binds normalized Base rows to immutable snapshot artifacts."""

    def __init__(self, connection: sqlite3.Connection, artifacts: ArtifactStore) -> None:
        if artifacts.connection is not connection:
            raise SnapshotStoreError(
                "SnapshotStore and ArtifactStore must share one database connection"
            )
        self.connection = connection
        self.artifacts = artifacts

    def cursor(self, source_id: str) -> IngestionCursor | None:
        row = self.connection.execute(
            "SELECT * FROM ingestion_cursors WHERE source_id = ?", (source_id,)
        ).fetchone()
        return None if row is None else self._cursor_record(row)

    def checkpoint(
        self,
        source_id: str,
        *,
        next_offset: int,
        partial_artifact_id: str,
        expected_version: int | None,
    ) -> IngestionCursor:
        if next_offset <= 0:
            raise SnapshotStoreError("checkpoint next_offset must be positive")
        if self.artifacts.get(partial_artifact_id) is None:
            raise SnapshotStoreError("checkpoint artifact is not registered")
        timestamp = _now()
        with transaction(self.connection, mode="IMMEDIATE"):
            current = self.cursor(source_id)
            if current is None:
                if expected_version not in {None, 0}:
                    raise SnapshotConflictError("ingestion cursor version changed")
                self.connection.execute(
                    "INSERT INTO ingestion_cursors("
                    "source_id, next_offset, partial_artifact_id, last_snapshot_id, "
                    "state_version, updated_at) VALUES (?, ?, ?, NULL, 1, ?)",
                    (source_id, next_offset, partial_artifact_id, timestamp),
                )
            else:
                if expected_version is None:
                    raise SnapshotConflictError(
                        "existing ingestion cursor requires expected_version"
                    )
                if current.state_version != expected_version:
                    raise SnapshotConflictError("ingestion cursor version changed")
                changed = self.connection.execute(
                    "UPDATE ingestion_cursors SET next_offset = ?, partial_artifact_id = ?, "
                    "state_version = state_version + 1, updated_at = ? "
                    "WHERE source_id = ? AND state_version = ?",
                    (
                        next_offset,
                        partial_artifact_id,
                        timestamp,
                        source_id,
                        current.state_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise SnapshotConflictError("ingestion cursor changed concurrently")
        result = self.cursor(source_id)
        if result is None:
            raise SnapshotStoreError("ingestion cursor disappeared")
        return result

    def commit(
        self,
        snapshot: IngestionSnapshot,
        *,
        expected_cursor_version: int | None = None,
        expected_next_offset: int | None = None,
    ) -> tuple[str, bool]:
        payload_bytes = snapshot.payload_json.encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        if snapshot.snapshot_id != payload_hash:
            raise SnapshotConflictError("snapshot_id must equal the payload SHA-256")
        if len(snapshot.accepted) + len(snapshot.rejected) != snapshot.source_record_count:
            raise SnapshotConflictError("snapshot counts do not reconcile")
        if snapshot.end_offset - snapshot.start_offset != snapshot.source_record_count:
            raise SnapshotConflictError("snapshot offset range does not reconcile")
        accepted_keys = [
            (record.source_record_id, record.record_revision)
            for record in snapshot.accepted
        ]
        accepted_base_ids = [record.base_record_id for record in snapshot.accepted]
        if len(accepted_keys) != len(set(accepted_keys)) or len(accepted_base_ids) != len(
            set(accepted_base_ids)
        ):
            raise SnapshotConflictError("snapshot contains duplicate accepted identities")
        for record in snapshot.accepted:
            try:
                expected_hash = source_content_hash(
                    source_id=snapshot.source_id,
                    table_id=snapshot.table_id,
                    view_id=snapshot.view_id,
                    base_record_id=record.base_record_id,
                    source_record_id=record.source_record_id,
                    fields_json=record.fields_json,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                raise SnapshotConflictError("accepted fields_json is invalid") from exc
            if record.content_hash != expected_hash:
                raise SnapshotConflictError(
                    f"accepted content_hash is invalid: {record.source_record_id}"
                )
        bound_payload = self._validate_payload_binding(snapshot)
        timestamp = _now()
        with transaction(self.connection, mode="IMMEDIATE"):
            self._prevalidate(snapshot, payload_hash)
            initial_cursor = self.cursor(snapshot.source_id)
            same_completed_snapshot = (
                initial_cursor is not None
                and initial_cursor.last_snapshot_id == snapshot.snapshot_id
                and initial_cursor.next_offset == 0
            )
            if initial_cursor is not None and not same_completed_snapshot:
                if expected_cursor_version is None:
                    raise SnapshotConflictError(
                        "commit requires the ingestion cursor version observed before reading"
                    )
                if initial_cursor.state_version != expected_cursor_version:
                    raise SnapshotConflictError("ingestion cursor version changed")
            if (
                initial_cursor is not None
                and expected_next_offset is not None
                and initial_cursor.next_offset != expected_next_offset
            ):
                raise SnapshotConflictError("ingestion cursor offset changed")
            if (
                initial_cursor is not None
                and initial_cursor.next_offset > 0
                and snapshot.end_offset != initial_cursor.next_offset
            ):
                raise SnapshotConflictError(
                    "snapshot end_offset does not match the partial cursor"
                )
            if initial_cursor is not None and initial_cursor.next_offset > 0:
                self._validate_partial_binding(initial_cursor, bound_payload)
            artifact = self.artifacts.put_bytes(
                payload_bytes,
                kind="feishu_ingestion_snapshot",
                media_type="application/json",
                metadata={
                    "schema_version": snapshot.schema_version,
                    "source_id": snapshot.source_id,
                    "snapshot_id": snapshot.snapshot_id,
                },
            )
            inserted = self.connection.execute(
                """
                INSERT INTO source_snapshots(
                    snapshot_id, source_id, artifact_id, payload_hash,
                    start_offset, end_offset, page_count, source_record_count,
                    accepted_count, rejected_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.source_id,
                    artifact.artifact_id,
                    payload_hash,
                    snapshot.start_offset,
                    snapshot.end_offset,
                    snapshot.page_count,
                    snapshot.source_record_count,
                    len(snapshot.accepted),
                    len(snapshot.rejected),
                    timestamp,
                ),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM source_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if row is None or row["payload_hash"] != payload_hash:
                raise SnapshotConflictError(
                    f"snapshot_id identifies different content: {snapshot.snapshot_id}"
                )

            for record in snapshot.accepted:
                self.connection.execute(
                    """
                    INSERT INTO source_record_revisions(
                        source_id, source_record_id, record_revision, base_record_id,
                        content_hash, normalized_json, first_snapshot_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, source_record_id, record_revision) DO NOTHING
                    """,
                    (
                        snapshot.source_id,
                        record.source_record_id,
                        record.record_revision,
                        record.base_record_id,
                        record.content_hash,
                        record.fields_json,
                        snapshot.snapshot_id,
                        timestamp,
                    ),
                )
                existing = self.connection.execute(
                    "SELECT base_record_id, content_hash, normalized_json "
                    "FROM source_record_revisions "
                    "WHERE source_id = ? AND source_record_id = ? AND record_revision = ?",
                    (
                        snapshot.source_id,
                        record.source_record_id,
                        record.record_revision,
                    ),
                ).fetchone()
                if (
                    existing is None
                    or existing["base_record_id"] != record.base_record_id
                    or existing["content_hash"] != record.content_hash
                    or existing["normalized_json"] != record.fields_json
                ):
                    raise SnapshotConflictError(
                        f"source revision identifies different content: {record.source_record_id}"
                    )

            for rejection_index, record in enumerate(snapshot.rejected):
                self.connection.execute(
                    """
                    INSERT INTO source_rejections(
                        snapshot_id, rejection_index, base_record_id, source_record_id,
                        record_revision, issue_codes_json, raw_content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id, rejection_index) DO NOTHING
                    """,
                    (
                        snapshot.snapshot_id,
                        rejection_index,
                        record.base_record_id,
                        record.source_record_id,
                        record.record_revision,
                        json.dumps(list(record.issue_codes), separators=(",", ":")),
                        record.raw_content_hash,
                        timestamp,
                    ),
                )
                existing_rejection = self.connection.execute(
                    "SELECT base_record_id, source_record_id, record_revision, "
                    "issue_codes_json, raw_content_hash FROM source_rejections "
                    "WHERE snapshot_id = ? AND rejection_index = ?",
                    (snapshot.snapshot_id, rejection_index),
                ).fetchone()
                expected_issues = json.dumps(
                    list(record.issue_codes), separators=(",", ":")
                )
                if existing_rejection is None or (
                    existing_rejection["base_record_id"] != record.base_record_id
                    or existing_rejection["source_record_id"] != record.source_record_id
                    or existing_rejection["record_revision"] != record.record_revision
                    or existing_rejection["issue_codes_json"] != expected_issues
                    or existing_rejection["raw_content_hash"] != record.raw_content_hash
                ):
                    raise SnapshotConflictError(
                        f"snapshot rejection changed at index {rejection_index}"
                    )

            current = self.cursor(snapshot.source_id)
            if current is None:
                self.connection.execute(
                    "INSERT INTO ingestion_cursors("
                    "source_id, next_offset, partial_artifact_id, last_snapshot_id, "
                    "state_version, updated_at) VALUES (?, 0, NULL, ?, 1, ?)",
                    (snapshot.source_id, snapshot.snapshot_id, timestamp),
                )
            elif current.last_snapshot_id != snapshot.snapshot_id or current.next_offset != 0:
                changed = self.connection.execute(
                    "UPDATE ingestion_cursors SET next_offset = 0, partial_artifact_id = NULL, "
                    "last_snapshot_id = ?, state_version = state_version + 1, updated_at = ? "
                    "WHERE source_id = ? AND state_version = ?",
                    (
                        snapshot.snapshot_id,
                        timestamp,
                        snapshot.source_id,
                        current.state_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise SnapshotConflictError("ingestion cursor changed concurrently")
        return artifact.artifact_id, bool(inserted)

    @staticmethod
    def _validate_payload_binding(snapshot: IngestionSnapshot) -> dict[str, object]:
        try:
            payload = json.loads(snapshot.payload_json)
        except json.JSONDecodeError as exc:
            raise SnapshotConflictError("snapshot payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SnapshotConflictError("snapshot payload root must be an object")
        source = payload.get("source")
        if not isinstance(source, dict):
            raise SnapshotConflictError("snapshot payload source must be an object")
        expected = {
            "schema_version": snapshot.schema_version,
            "start_offset": snapshot.start_offset,
            "end_offset": snapshot.end_offset,
            "page_count": snapshot.page_count,
            "source_record_count": snapshot.source_record_count,
            "accepted": [record.to_payload() for record in snapshot.accepted],
            "rejected": [record.to_payload() for record in snapshot.rejected],
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise SnapshotConflictError(
                    f"snapshot payload does not match normalized {key}"
                )
        if source.get("source_id") != snapshot.source_id:
            raise SnapshotConflictError("snapshot payload source does not match provenance")
        digest = source.get("registration_digest")
        if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise SnapshotConflictError("snapshot payload registration digest is invalid")
        return payload

    def _validate_partial_binding(
        self,
        cursor: IngestionCursor,
        snapshot_payload: dict[str, object],
    ) -> None:
        if cursor.partial_artifact_id is None:
            raise SnapshotConflictError("advanced cursor has no partial artifact")
        try:
            partial = json.loads(
                self.artifacts.read_bytes(cursor.partial_artifact_id).decode("utf-8")
            )
        except Exception as exc:
            raise SnapshotConflictError("partial cursor artifact is invalid") from exc
        source = snapshot_payload.get("source")
        if not isinstance(partial, dict) or not isinstance(source, dict):
            raise SnapshotConflictError("partial cursor artifact root is invalid")
        if (
            partial.get("schema_version") != "feishu_ingestion_partial.v1"
            or partial.get("source_id") != cursor.source_id
            or partial.get("registration_digest") != source.get("registration_digest")
            or partial.get("next_offset") != cursor.next_offset
        ):
            raise SnapshotConflictError("partial cursor provenance does not match snapshot")

    def _prevalidate(self, snapshot: IngestionSnapshot, payload_hash: str) -> None:
        existing_snapshot = self.connection.execute(
            "SELECT payload_hash FROM source_snapshots WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if existing_snapshot is not None and existing_snapshot["payload_hash"] != payload_hash:
            raise SnapshotConflictError(
                f"snapshot_id identifies different content: {snapshot.snapshot_id}"
            )
        for record in snapshot.accepted:
            existing = self.connection.execute(
                "SELECT base_record_id, content_hash, normalized_json "
                "FROM source_record_revisions "
                "WHERE source_id = ? AND source_record_id = ? AND record_revision = ?",
                (
                    snapshot.source_id,
                    record.source_record_id,
                    record.record_revision,
                ),
            ).fetchone()
            if existing is not None and (
                existing["base_record_id"] != record.base_record_id
                or existing["content_hash"] != record.content_hash
                or existing["normalized_json"] != record.fields_json
            ):
                raise SnapshotConflictError(
                    f"source revision identifies different content: {record.source_record_id}"
                )

    @staticmethod
    def _cursor_record(row: sqlite3.Row) -> IngestionCursor:
        return IngestionCursor(
            source_id=str(row["source_id"]),
            next_offset=int(row["next_offset"]),
            partial_artifact_id=(
                None if row["partial_artifact_id"] is None else str(row["partial_artifact_id"])
            ),
            last_snapshot_id=(
                None if row["last_snapshot_id"] is None else str(row["last_snapshot_id"])
            ),
            state_version=int(row["state_version"]),
            updated_at=str(row["updated_at"]),
        )
