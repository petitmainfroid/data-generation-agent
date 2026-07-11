from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_generation_agent.feishu.models import (
    BasePage,
    BaseReadError,
    BaseRecord,
    BaseRequestError,
)
from data_generation_agent.feishu.runtime import ingest_registered_source
from data_generation_agent.harness.db import initialize_database


class FakeClient:
    def list_records(self, **kwargs) -> BasePage:
        fields = {
            field_id: ("A bounded seed question" if index == 0 else None)
            for index, field_id in enumerate(kwargs["field_ids"])
        }
        # Select by the runtime registration's sorted field order: ensure the
        # actual question field, not the first arbitrary field, has content.
        fields["fldQuestion123"] = "A bounded seed question"
        return BasePage(
            records=(BaseRecord("rec-1", "revision-1", fields),),
            has_more=False,
        )


class ScriptedClient:
    def __init__(self, pages) -> None:
        self.pages = dict(pages)
        self.offsets: list[int] = []

    def list_records(self, **kwargs) -> BasePage:
        offset = kwargs["offset"]
        self.offsets.append(offset)
        value = self.pages[offset]
        if isinstance(value, Exception):
            raise value
        return value


def _profile(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "feishu_base_profile.v1",
                "base_alias": "development",
                "environment": "development",
                "identity": "user",
                "base_token": "local-secret-token",
                "tables": {
                    "seeds": {
                        "table_id": "tblSeed123",
                        "fields": {
                            "question": "fldQuestion123",
                            "sft_id": "fldSft123",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_runtime_commits_a_resumable_snapshot_without_persisting_token(tmp_path: Path) -> None:
    profile = tmp_path / "profile.local.json"
    _profile(profile)
    database = tmp_path / "harness.sqlite3"
    artifacts = tmp_path / "artifacts"
    first = ingest_registered_source(
        profile_path=profile,
        database_path=database,
        artifact_root=artifacts,
        client=FakeClient(),
    )
    second = ingest_registered_source(
        profile_path=profile,
        database_path=database,
        artifact_root=artifacts,
        client=FakeClient(),
    )
    assert first["accepted_count"] == 1
    assert first["rejected_count"] == 0
    assert first["created"] is True
    assert second["created"] is False
    artifact_bytes = b"".join(
        path.read_bytes() for path in artifacts.rglob("*") if path.is_file()
    )
    assert b"local-secret-token" not in artifact_bytes
    assert b"tblSeed123" not in artifact_bytes
    assert b"fldQuestion123" not in artifact_bytes
    assert b"fldSft123" not in artifact_bytes
    connection = initialize_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 1
        for table in ("source_snapshots", "source_record_revisions", "ingestion_cursors"):
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            assert "local-secret-token" not in repr([dict(row) for row in rows])
    finally:
        connection.close()


def test_runtime_resumes_after_first_page_without_rereading_it(tmp_path: Path) -> None:
    profile = tmp_path / "profile.local.json"
    _profile(profile)
    database = tmp_path / "harness.sqlite3"
    artifacts = tmp_path / "artifacts"
    first_record = BaseRecord(
        "rec-1",
        "revision-1",
        {"fldQuestion123": "Question one", "fldSft123": None},
    )
    second_record = BaseRecord(
        "rec-2",
        "revision-2",
        {"fldQuestion123": "Question two", "fldSft123": "seed-2"},
    )
    first_client = ScriptedClient(
        {
            0: BasePage((first_record,), True),
            1: BaseRequestError(400, error_code="injected_crash"),
        }
    )
    with pytest.raises(BaseReadError):
        ingest_registered_source(
            profile_path=profile,
            database_path=database,
            artifact_root=artifacts,
            page_size=1,
            client=first_client,
        )
    assert first_client.offsets == [0, 1]
    interrupted = initialize_database(database)
    try:
        row = interrupted.execute(
            "SELECT next_offset, partial_artifact_id FROM ingestion_cursors"
        ).fetchone()
        assert row["next_offset"] == 1
        assert row["partial_artifact_id"] is not None
    finally:
        interrupted.close()

    resumed_client = ScriptedClient({1: BasePage((second_record,), False)})
    result = ingest_registered_source(
        profile_path=profile,
        database_path=database,
        artifact_root=artifacts,
        page_size=1,
        client=resumed_client,
    )
    assert resumed_client.offsets == [1]
    assert result["source_record_count"] == 2
    assert result["accepted_count"] == 2
    completed = initialize_database(database)
    try:
        cursor = completed.execute(
            "SELECT next_offset, partial_artifact_id, last_snapshot_id "
            "FROM ingestion_cursors"
        ).fetchone()
        assert cursor["next_offset"] == 0
        assert cursor["partial_artifact_id"] is None
        assert cursor["last_snapshot_id"] == result["snapshot_id"]
    finally:
        completed.close()


def test_runtime_never_checkpoints_an_echoed_profile_token(tmp_path: Path) -> None:
    profile = tmp_path / "profile.local.json"
    _profile(profile)
    database = tmp_path / "harness.sqlite3"
    artifacts = tmp_path / "artifacts"
    poisoned = BaseRecord(
        "rec-secret",
        "revision-secret",
        {
            "fldQuestion123": "local-secret-token",
            "fldSft123": None,
        },
    )
    with pytest.raises(BaseReadError, match="credential material"):
        ingest_registered_source(
            profile_path=profile,
            database_path=database,
            artifact_root=artifacts,
            client=ScriptedClient({0: BasePage((poisoned,), False)}),
        )
    assert not any(path.is_file() for path in artifacts.rglob("*"))
    connection = initialize_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ingestion_cursors").fetchone()[0] == 0
    finally:
        connection.close()
