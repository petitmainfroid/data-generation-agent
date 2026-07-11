from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from data_generation_agent.feishu.ingestion import (
    FeishuIngestionService,
    SourceRegistry,
    normalize_question,
)
from data_generation_agent.feishu.models import (
    BasePage,
    BaseReadError,
    BaseRecord,
    BaseRequestError,
    PaginationError,
    RetryPolicy,
    SourceRegistration,
    SourceRegistrationError,
    UnknownSourceError,
)


TOKEN = "tenant-token-must-never-appear"


class FakeBaseClient:
    def __init__(self, scripted: dict[int, list[BasePage | Exception]]) -> None:
        self.scripted = {offset: list(values) for offset, values in scripted.items()}
        self.calls: list[dict[str, Any]] = []
        self.write_calls = 0

    def list_records(self, **kwargs: Any) -> BasePage:
        self.calls.append(dict(kwargs))
        offset = int(kwargs["offset"])
        values = self.scripted[offset]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    def update_record(self, **_: Any) -> None:
        self.write_calls += 1
        raise AssertionError("ingestion must never write Base")


def _registration(**overrides: Any) -> SourceRegistration:
    values: dict[str, Any] = {
        "source_id": "dev-seeds",
        "base_token": TOKEN,
        "table_id": "tbl_seed",
        "view_id": "vew_ready",
        "field_allowlist": {
            "question": "fld_question",
            "source_id": "fld_source_id",
            "notes": "fld_notes",
        },
        "question_key": "question",
        "identity_key": "source_id",
        "page_size": 2,
    }
    values.update(overrides)
    return SourceRegistration(**values)


def _record(
    record_id: str,
    source_id: str,
    question: Any,
    *,
    revision: str | int | None = "1",
    notes: Any = "note",
    extra: Any = "must-not-be-read",
) -> BaseRecord:
    return BaseRecord(
        record_id=record_id,
        revision=revision,
        fields={
            "fld_question": question,
            "fld_source_id": source_id,
            "fld_notes": notes,
            "fld_unregistered_secret": extra,
        },
    )


def _service(client: FakeBaseClient, **kwargs: Any) -> FeishuIngestionService:
    return FeishuIngestionService(
        client,
        SourceRegistry([_registration()]),
        **kwargs,
    )


def test_full_offset_pagination_normalizes_questions_and_reads_only_allowlist() -> None:
    client = FakeBaseClient(
        {
            0: [BasePage((_record("rec-1", "seed-1", "  Ａ　方案\r\n\r\n B  "),), True)],
            1: [BasePage((_record("rec-2", "seed-2", [{"text": "第二"}, {"text": "题"}]),), False)],
        }
    )
    snapshot = _service(client).ingest("dev-seeds", cursor="0")

    assert [call["offset"] for call in client.calls] == [0, 1]
    assert all(call["limit"] == 2 for call in client.calls)
    assert all(
        call["field_ids"] == ("fld_notes", "fld_question", "fld_source_id")
        for call in client.calls
    )
    assert snapshot.page_count == 2
    assert snapshot.start_offset == 0
    assert snapshot.end_offset == 2
    assert snapshot.source_record_count == 2
    assert [record.question for record in snapshot.accepted] == ["A 方案\n\nB", "第二题"]
    assert snapshot.rejected == ()
    assert "fld_unregistered_secret" not in snapshot.payload_json
    assert "must-not-be-read" not in snapshot.payload_json
    assert TOKEN not in snapshot.payload_json
    assert TOKEN not in repr(_registration())
    assert client.write_calls == 0


def test_offset_cursor_starts_at_registered_read_position() -> None:
    client = FakeBaseClient(
        {5: [BasePage((_record("rec-5", "seed-5", "Question"),), False)]}
    )
    snapshot = _service(client).ingest("dev-seeds", cursor=5)
    assert [call["offset"] for call in client.calls] == [5]
    assert snapshot.start_offset == 5
    assert snapshot.end_offset == 6


def test_429_and_5xx_retry_are_bounded_and_repeat_the_same_offset() -> None:
    delays: list[float] = []
    client = FakeBaseClient(
        {
            0: [
                BaseRequestError(429, retry_after_seconds=0.05),
                BaseRequestError(503),
                BasePage((_record("rec-1", "seed-1", "Question"),), False),
            ]
        }
    )
    snapshot = _service(
        client,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=1),
        sleeper=delays.append,
    ).ingest("dev-seeds")
    assert snapshot.source_record_count == 1
    assert [call["offset"] for call in client.calls] == [0, 0, 0]
    assert delays == [0.05, 0.2]

    exhausted = FakeBaseClient({0: [BaseRequestError(500), BaseRequestError(502), BaseRequestError(503)]})
    with pytest.raises(BaseReadError, match="status=503; attempts=3"):
        _service(
            exhausted,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
            sleeper=lambda _: None,
        ).ingest("dev-seeds")
    assert len(exhausted.calls) == 3


def test_non_retryable_error_and_unexpected_error_do_not_leak_token() -> None:
    bad_request = FakeBaseClient({0: [BaseRequestError(400, error_code="bad_request")]})
    with pytest.raises(BaseReadError) as captured:
        _service(bad_request).ingest("dev-seeds")
    assert len(bad_request.calls) == 1
    assert TOKEN not in str(captured.value)

    unexpected = FakeBaseClient({0: [RuntimeError(f"bad response for {TOKEN}")]})
    with pytest.raises(BaseReadError) as unexpected_error:
        _service(unexpected).ingest("dev-seeds")
    assert TOKEN not in str(unexpected_error.value)
    assert unexpected_error.value.__cause__ is None

    echoed_credential = FakeBaseClient(
        {0: [BasePage((_record("rec-1", "seed-1", "Question", notes=TOKEN),), False)]}
    )
    with pytest.raises(BaseReadError) as credential_error:
        _service(echoed_credential).ingest("dev-seeds")
    assert TOKEN not in str(credential_error.value)


def test_duplicate_empty_encoding_and_missing_revision_are_explicit_rejections() -> None:
    records = (
        _record("rec-1", "duplicate", "Valid one"),
        _record("rec-2", "duplicate", "Valid two"),
        _record("rec-3", "empty", " \r\n "),
        _record("rec-4", "encoding", "broken \ufffd text"),
        _record("rec-5", "no-revision", "Valid", revision=None),
        _record("rec-6", "nested-encoding", "Valid", notes={"text": "bad\ufffd"}),
    )
    snapshot = _service(FakeBaseClient({0: [BasePage(records, False)]})).ingest("dev-seeds")
    assert snapshot.accepted == ()
    issues = {record.base_record_id: set(record.issue_codes) for record in snapshot.rejected}
    assert issues["rec-1"] == {"DUPLICATE_ID"}
    assert issues["rec-2"] == {"DUPLICATE_ID"}
    assert issues["rec-3"] == {"EMPTY_QUESTION"}
    assert issues["rec-4"] == {"ENCODING_ANOMALY"}
    assert issues["rec-5"] == {"MISSING_RECORD_REVISION"}
    assert issues["rec-6"] == {"ENCODING_ANOMALY"}


def test_snapshot_is_deterministic_immutable_and_revision_is_not_content_hash() -> None:
    first_records = (
        _record("rec-b", "seed-b", "Question B", revision="rev-1"),
        _record("rec-a", "seed-a", "Question A", revision="rev-1"),
    )
    second_records = tuple(reversed(first_records))
    first = _service(FakeBaseClient({0: [BasePage(first_records, False)]})).ingest("dev-seeds")
    second = _service(FakeBaseClient({0: [BasePage(second_records, False)]})).ingest("dev-seeds")
    assert first.snapshot_id == second.snapshot_id
    assert first.payload_json == second.payload_json
    with pytest.raises(FrozenInstanceError):
        first.snapshot_id = "changed"  # type: ignore[misc]

    changed_revision = _service(
        FakeBaseClient(
            {0: [BasePage((_record("rec-a", "seed-a", "Question A", revision="rev-2"),), False)]}
        )
    ).ingest("dev-seeds")
    original_a = next(record for record in first.accepted if record.source_record_id == "seed-a")
    assert changed_revision.accepted[0].record_revision == "rev-2"
    assert changed_revision.accepted[0].content_hash == original_a.content_hash


def test_unknown_source_invalid_cursor_and_stalled_pagination_fail_closed() -> None:
    client = FakeBaseClient({0: [BasePage((), False)]})
    service = _service(client)
    with pytest.raises(UnknownSourceError):
        service.ingest("not-registered")
    assert client.calls == []
    with pytest.raises(PaginationError, match="non-negative integer"):
        service.ingest("dev-seeds", cursor="not-an-offset")

    stalled = FakeBaseClient({0: [BasePage((), True)]})
    with pytest.raises(PaginationError, match="made no progress"):
        _service(stalled).ingest("dev-seeds")


def test_source_registry_rejects_duplicate_or_unallowlisted_required_fields() -> None:
    with pytest.raises(SourceRegistrationError, match="duplicate source"):
        SourceRegistry([_registration(), _registration()])
    with pytest.raises(SourceRegistrationError, match="question_key"):
        _registration(field_allowlist={"source_id": "fld_source_id"})
    assert _registration(page_size=200).page_size == 200
    with pytest.raises(SourceRegistrationError, match="between 1 and 200"):
        _registration(page_size=201)


def test_question_normalization_rejects_invalid_types_and_encoding() -> None:
    assert normalize_question("  Ａ  \r\n B ") == "A\nB"
    with pytest.raises(TypeError):
        normalize_question(123)
    with pytest.raises(UnicodeError):
        normalize_question("bad\ufffd")
