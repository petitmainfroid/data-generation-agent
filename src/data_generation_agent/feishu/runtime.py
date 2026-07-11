from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import initialize_database

from .client import LarkCliBaseClient, LarkCliIngestionClient
from .ingestion import FeishuIngestionService, SourceRegistry
from .models import BaseClient, BaseReadError, BaseRecord, SourceRegistration
from .profile import BaseProfile
from .snapshots import SnapshotStore


PARTIAL_SCHEMA_VERSION = "feishu_ingestion_partial.v1"


def _partial_payload(
    registration: SourceRegistration,
    *,
    next_offset: int,
    page_count: int,
    records: tuple[BaseRecord, ...],
) -> dict[str, Any]:
    return {
        "schema_version": PARTIAL_SCHEMA_VERSION,
        "source_id": registration.source_id,
        "registration_digest": registration.public_payload()["registration_digest"],
        "next_offset": next_offset,
        "page_count": page_count,
        "records": [
            {
                "record_id": record.record_id,
                "revision": record.revision,
                "fields": {
                    alias: record.fields.get(field_id)
                    for alias, field_id in registration.field_allowlist.items()
                },
            }
            for record in records
        ],
    }


def _load_partial(
    registration: SourceRegistration,
    artifacts: ArtifactStore,
    artifact_id: str,
    *,
    expected_offset: int,
) -> tuple[tuple[BaseRecord, ...], int]:
    try:
        payload = json.loads(artifacts.read_bytes(artifact_id).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("ingestion partial artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("ingestion partial artifact root must be an object")
    if (
        payload.get("schema_version") != PARTIAL_SCHEMA_VERSION
        or payload.get("source_id") != registration.source_id
        or payload.get("registration_digest")
        != registration.public_payload()["registration_digest"]
        or payload.get("next_offset") != expected_offset
    ):
        raise ValueError("ingestion partial artifact provenance does not match cursor")
    page_count = payload.get("page_count")
    raw_records = payload.get("records")
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 1
        or not isinstance(raw_records, list)
    ):
        raise ValueError("ingestion partial artifact pagination is invalid")
    allowed_aliases = set(registration.field_allowlist)
    records: list[BaseRecord] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("record_id"), str):
            raise ValueError("ingestion partial artifact record is invalid")
        aliases = raw.get("fields")
        if not isinstance(aliases, Mapping) or set(aliases) != allowed_aliases:
            raise ValueError("ingestion partial artifact fields do not match registration")
        fields = {
            registration.field_allowlist[alias]: aliases[alias]
            for alias in registration.field_allowlist
        }
        records.append(
            BaseRecord(
                record_id=raw["record_id"],
                revision=raw.get("revision"),
                fields=fields,
            )
        )
    if len(records) != expected_offset:
        raise ValueError("ingestion partial record count does not match cursor offset")
    return tuple(records), page_count


def registration_from_profile(
    profile: BaseProfile,
    *,
    table_alias: str,
    source_id: str | None = None,
    view_alias: str | None = None,
    page_size: int = 100,
) -> SourceRegistration:
    table = profile.table(table_alias)
    if "question" not in table.fields:
        raise ValueError(f"registered table {table_alias!r} has no question field")
    view_id = None if view_alias is None else table.view_id(view_alias)
    return SourceRegistration(
        source_id=source_id or f"{profile.alias}:{table_alias}",
        base_token=profile.base_token,
        table_id=table.table_id,
        view_id=view_id,
        field_allowlist=dict(table.fields),
        question_key="question",
        # The Base record ID is always available and stable. User-provided
        # sft_id remains an optional field, so users only need to fill question.
        identity_key=None,
        page_size=page_size,
    )


def ingest_registered_source(
    *,
    profile_path: Path,
    database_path: Path,
    artifact_root: Path,
    table_alias: str = "seeds",
    source_id: str | None = None,
    view_alias: str | None = None,
    page_size: int = 100,
    client: BaseClient | None = None,
) -> dict[str, Any]:
    profile = BaseProfile.load(profile_path)
    registration = registration_from_profile(
        profile,
        table_alias=table_alias,
        source_id=source_id,
        view_alias=view_alias,
        page_size=page_size,
    )
    read_client = client or LarkCliIngestionClient(LarkCliBaseClient(profile))
    service = FeishuIngestionService(read_client, SourceRegistry([registration]))
    connection = initialize_database(database_path)
    try:
        artifacts = ArtifactStore(artifact_root, connection)
        store = SnapshotStore(connection, artifacts)
        observed_cursor = store.cursor(registration.source_id)
        prior_records: tuple[BaseRecord, ...] = ()
        prior_page_count = 0
        resume_offset = 0
        if observed_cursor is not None and observed_cursor.next_offset > 0:
            if observed_cursor.partial_artifact_id is None:
                raise ValueError("advanced ingestion cursor has no partial artifact")
            resume_offset = observed_cursor.next_offset
            prior_records, prior_page_count = _load_partial(
                registration,
                artifacts,
                observed_cursor.partial_artifact_id,
                expected_offset=resume_offset,
            )

        def checkpoint(
            next_offset: int,
            page_count: int,
            records: tuple[BaseRecord, ...],
        ) -> None:
            nonlocal observed_cursor
            partial_payload = _partial_payload(
                registration,
                next_offset=next_offset,
                page_count=page_count,
                records=records,
            )
            serialized_partial = json.dumps(
                partial_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if registration.base_token in serialized_partial:
                raise BaseReadError(
                    f"partial snapshot contained credential material for registered source {registration.source_id}"
                )
            partial = artifacts.put_json(
                partial_payload,
                kind="feishu_ingestion_partial",
                metadata={"source_id": registration.source_id},
            )
            observed_cursor = store.checkpoint(
                registration.source_id,
                next_offset=next_offset,
                partial_artifact_id=partial.artifact_id,
                expected_version=(
                    None if observed_cursor is None else observed_cursor.state_version
                ),
            )

        snapshot = service.ingest(
            registration.source_id,
            cursor=resume_offset,
            prior_records=prior_records,
            prior_page_count=prior_page_count,
            snapshot_start_offset=0,
            on_page=checkpoint,
        )
        artifact_id, created = store.commit(
            snapshot,
            expected_cursor_version=(
                None if observed_cursor is None else observed_cursor.state_version
            ),
            expected_next_offset=(
                0 if observed_cursor is None else observed_cursor.next_offset
            ),
        )
    finally:
        connection.close()
    return {
        "ok": True,
        "source_id": snapshot.source_id,
        "snapshot_id": snapshot.snapshot_id,
        "artifact_id": artifact_id,
        "created": created,
        "page_count": snapshot.page_count,
        "source_record_count": snapshot.source_record_count,
        "accepted_count": len(snapshot.accepted),
        "rejected_count": len(snapshot.rejected),
        "end_offset": snapshot.end_offset,
    }
