from __future__ import annotations

import json
import subprocess
from copy import deepcopy

import pytest

from data_generation_agent.feishu.client import (
    LarkCliBaseClient,
    LarkCliCommandError,
    LarkCliProtocolError,
    LarkCliRateLimitError,
    LarkCliTimeoutError,
    LarkCliIngestionClient,
)
from data_generation_agent.feishu.ingestion import FeishuIngestionService, SourceRegistry
from data_generation_agent.feishu.models import BaseClient, BaseRequestError, SourceRegistration
from data_generation_agent.feishu.profile import BaseProfile, UnregisteredResourceError


TOKEN = "bascn-super-secret-client-token"


def _profile() -> BaseProfile:
    return BaseProfile.from_mapping(
        {
            "schema_version": "feishu_base_profile.v1",
            "base_alias": "development",
            "environment": "development",
            "identity": "user",
            "base_token": TOKEN,
            "tables": {
                "seeds": {
                    "table_id": "tblSeed123",
                    "fields": {
                        "question": "fldQuestion123",
                        "status": "fldStatus123",
                    },
                    "views": {"pending": "vewPending123"},
                }
            },
        }
    )


class FakeRunner:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, *, timeout: float):
        self.calls.append((list(argv), timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _completed(payload: object, *, returncode: int = 0, stderr: str = ""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return subprocess.CompletedProcess(["lark-cli"], returncode, stdout=stdout, stderr=stderr)


def test_record_list_uses_only_fixed_alias_resolved_argv_and_offset_cursor() -> None:
    envelope = {
        "ok": True,
        "identity": "user",
        "_notice": {"message": f"safe; base_token={TOKEN}"},
        "data": {
            "data": [
                ["Q1", "ready"],
                ["Q2", "pending"],
            ],
            "field_id_list": ["fldQuestion123", "fldStatus123"],
            "fields": [
                {"field_id": "fldQuestion123", "field_name": "题目"},
                {"field_id": "fldStatus123", "field_name": "状态"},
            ],
            "has_more": True,
            "query_context": {"view_id": "vewPending123"},
            "record_id_list": ["rec1", "rec2"],
        },
    }
    runner = FakeRunner(_completed(envelope))
    client = LarkCliBaseClient(_profile(), runner=runner, timeout_seconds=12)

    page = client.list_records(
        "seeds",
        field_aliases=["question", "status"],
        view_alias="pending",
        base_alias="development",
        offset=4,
        limit=2,
    )

    assert runner.calls == [
        (
            [
                "lark-cli",
                "base",
                "+record-list",
                "--base-token",
                TOKEN,
                "--table-id",
                "tblSeed123",
                "--as",
                "user",
                "--view-id",
                "vewPending123",
                "--field-id",
                "fldQuestion123",
                "--field-id",
                "fldStatus123",
                "--offset",
                "4",
                "--limit",
                "2",
                "--format",
                "json",
            ],
            12.0,
        )
    ]
    assert page.record_id_list == ("rec1", "rec2")
    assert page.field_id_list == ("fldQuestion123", "fldStatus123")
    assert page.records[0] == {
        "record_id": "rec1",
        "fields": {"fldQuestion123": "Q1", "fldStatus123": "ready"},
    }
    assert page.has_more is True
    assert page.offset == 4
    assert page.cursor == 6
    assert TOKEN not in json.dumps(page.notice)
    assert "[REDACTED]" in json.dumps(page.notice)
    assert TOKEN not in repr(client)


def test_field_list_uses_confirmed_shortcut_and_parses_page() -> None:
    runner = FakeRunner(
        _completed(
            {
                "ok": True,
                "identity": "user",
                "data": {
                    "fields": [
                        {"field_id": "fldQuestion123", "field_name": "题目"},
                        {"field_id": "fldStatus123", "field_name": "状态"},
                    ],
                    "total": 2,
                },
            }
        )
    )
    client = LarkCliBaseClient(_profile(), runner=runner)
    page = client.list_fields("seeds", offset=0, limit=200)

    argv, timeout = runner.calls[0]
    assert argv == [
        "lark-cli",
        "base",
        "+field-list",
        "--base-token",
        TOKEN,
        "--table-id",
        "tblSeed123",
        "--as",
        "user",
        "--offset",
        "0",
        "--limit",
        "200",
        "--format",
        "json",
    ]
    assert timeout == 30.0
    assert len(page.fields) == 2
    assert page.cursor is None


def test_record_list_accepts_mapping_rows_but_normalizes_to_registered_columns() -> None:
    runner = FakeRunner(
        _completed(
            {
                "ok": True,
                "identity": "user",
                "data": {
                    "data": [
                        {
                            "record_id": "rec1",
                            "revision": "rev-7",
                            "fields": {
                                "fldQuestion123": "Q1",
                                "fldStatus123": "ready",
                                "fldUnexpected123": "must be dropped",
                            },
                        }
                    ],
                    "record_id_list": ["rec1"],
                    "field_id_list": ["fldQuestion123", "fldStatus123"],
                    "fields": ["题目", "状态"],
                    "has_more": False,
                },
            }
        )
    )
    page = LarkCliBaseClient(_profile(), runner=runner).list_records(
        "seeds", field_aliases=("question", "status")
    )
    assert page.records == (
        {
            "record_id": "rec1",
            "revision": "rev-7",
            "fields": {"fldQuestion123": "Q1", "fldStatus123": "ready"},
        },
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(record_id_list=[]),
        lambda data: data.update(fields=[]),
        lambda data: data["data"].__setitem__(0, ["only-one-column"]),
        lambda data: data.update(field_id_list=["fldQuestion123", "fldQuestion123"]),
    ],
)
def test_record_list_parallel_array_mismatches_fail_closed(mutation) -> None:
    data = {
        "data": [["Q1", "ready"]],
        "record_id_list": ["rec1"],
        "field_id_list": ["fldQuestion123", "fldStatus123"],
        "fields": ["题目", "状态"],
        "has_more": False,
    }
    mutation(data)
    runner = FakeRunner(
        _completed({"ok": True, "identity": "user", "data": deepcopy(data)})
    )
    with pytest.raises(LarkCliProtocolError):
        LarkCliBaseClient(_profile(), runner=runner).list_records(
            "seeds", field_aliases=("question", "status")
        )


def test_field_list_total_drives_pagination_without_has_more() -> None:
    runner = FakeRunner(
        _completed(
            {
                "ok": True,
                "identity": "user",
                "data": {
                    "fields": [
                        {"field_id": "fldQuestion123", "field_name": "题目"}
                    ],
                    "total": 2,
                },
            }
        )
    )
    page = LarkCliBaseClient(_profile(), runner=runner).list_fields(
        "seeds", offset=0, limit=1
    )
    assert page.has_more is True
    assert page.cursor == 1


def test_unregistered_aliases_fail_before_runner_is_called() -> None:
    runner = FakeRunner()
    client = LarkCliBaseClient(_profile(), runner=runner)
    calls = (
        lambda: client.list_records("unknown"),
        lambda: client.list_records("seeds", field_aliases=["unknown"]),
        lambda: client.list_records("seeds", view_alias="unknown"),
        lambda: client.list_fields("seeds", base_alias="production"),
    )
    for call in calls:
        with pytest.raises(UnregisteredResourceError) as captured:
            call()
        assert TOKEN not in str(captured.value)
    assert runner.calls == []


@pytest.mark.parametrize("limit", [0, 201, True])
def test_invalid_page_limits_fail_before_runner(limit) -> None:
    runner = FakeRunner()
    client = LarkCliBaseClient(_profile(), runner=runner)
    with pytest.raises((TypeError, ValueError)):
        client.list_records("seeds", limit=limit)
    assert runner.calls == []


def test_timeout_and_nonzero_errors_are_standardized_and_token_free() -> None:
    timeout_runner = FakeRunner(
        subprocess.TimeoutExpired(["lark-cli", "--base-token", TOKEN], timeout=1)
    )
    with pytest.raises(LarkCliTimeoutError) as timeout_error:
        LarkCliBaseClient(_profile(), runner=timeout_runner).list_fields("seeds")
    assert TOKEN not in str(timeout_error.value)

    failed_runner = FakeRunner(
        _completed(
            "",
            returncode=2,
            stderr=f"request failed api_key=plain-secret base_token={TOKEN}",
        )
    )
    with pytest.raises(LarkCliCommandError) as command_error:
        LarkCliBaseClient(_profile(), runner=failed_runner).list_fields("seeds")
    message = str(command_error.value)
    assert TOKEN not in message
    assert "plain-secret" not in message
    assert message.count("[REDACTED]") >= 2


@pytest.mark.parametrize(
    "response",
    [
        _completed("", returncode=1, stderr="HTTP 429 too many requests"),
        _completed(
            {
                "ok": False,
                "identity": "user",
                "code": 1254291,
                "message": "rate limit",
                "data": {},
            }
        ),
    ],
)
def test_429_is_normalized_as_retryable_rate_limit(response) -> None:
    runner = FakeRunner(response)
    with pytest.raises(LarkCliRateLimitError) as captured:
        LarkCliBaseClient(_profile(), runner=runner).list_fields("seeds")
    assert TOKEN not in str(captured.value)


def test_malformed_json_and_error_envelope_cannot_leak_token() -> None:
    malformed = FakeRunner(_completed("not-json " + TOKEN))
    with pytest.raises(LarkCliProtocolError) as malformed_error:
        LarkCliBaseClient(_profile(), runner=malformed).list_fields("seeds")
    assert TOKEN not in str(malformed_error.value)

    envelope = FakeRunner(
        _completed(
            {
                "ok": False,
                "identity": "user",
                "message": f"permission denied for {TOKEN}",
                "_notice": {"api_key": "another-secret"},
                "data": {},
            }
        )
    )
    with pytest.raises(LarkCliCommandError) as envelope_error:
        LarkCliBaseClient(_profile(), runner=envelope).list_fields("seeds")
    message = str(envelope_error.value)
    assert TOKEN not in message
    assert "another-secret" not in message


def test_default_runner_explicitly_disables_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_subprocess_run(argv, **kwargs):
        observed["argv"] = list(argv)
        observed.update(kwargs)
        return _completed({"ok": True, "identity": "user", "data": []})

    monkeypatch.setattr("data_generation_agent.feishu.client.subprocess.run", fake_subprocess_run)
    monkeypatch.setattr(
        "data_generation_agent.feishu.client._lark_cli_prefix",
        lambda: ["native-lark-cli"],
    )
    client = LarkCliBaseClient(_profile())
    client.list_fields("seeds")

    assert observed["shell"] is False
    assert observed["check"] is False
    assert observed["argv"][:3] == ["native-lark-cli", "base", "+field-list"]
    assert "--base-token" in observed["argv"]


def test_ingestion_adapter_integrates_cli_profile_and_derives_stable_revision() -> None:
    response = {
        "ok": True,
        "identity": "user",
        "data": {
            "data": [["  A  \r\n B  "]],
            "record_id_list": ["rec1"],
            "field_id_list": ["fldQuestion123"],
            "fields": [{"field_id": "fldQuestion123", "field_name": "题目"}],
            "has_more": False,
        },
    }
    runner = FakeRunner(_completed(response))
    adapter = LarkCliIngestionClient(LarkCliBaseClient(_profile(), runner=runner))
    registration = SourceRegistration(
        source_id="dev-seeds",
        base_token=TOKEN,
        table_id="tblSeed123",
        view_id="vewPending123",
        field_allowlist={"question": "fldQuestion123"},
        question_key="question",
        identity_key=None,
        page_size=100,
    )

    snapshot = FeishuIngestionService(
        adapter, SourceRegistry((registration,))
    ).ingest("dev-seeds")

    assert isinstance(adapter, BaseClient)
    assert len(snapshot.accepted) == 1
    accepted = snapshot.accepted[0]
    assert accepted.source_record_id == "rec1"
    assert accepted.question == "A\nB"
    assert len(accepted.record_revision) == 64
    assert all(character in "0123456789abcdef" for character in accepted.record_revision)
    assert TOKEN not in snapshot.payload_json
    assert TOKEN not in repr(adapter)
    argv, _ = runner.calls[0]
    assert argv.count("--field-id") == 1
    assert "fldQuestion123" in argv
    assert "fldStatus123" not in argv


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_token": "unregistered-token"},
        {"table_id": "tblUnknown123"},
        {"view_id": "vewUnknown123"},
        {"field_ids": ("fldUnknown123",)},
        {"field_ids": ("fldQuestion123", "fldQuestion123")},
    ],
)
def test_ingestion_adapter_rejects_unregistered_ids_before_cli(overrides) -> None:
    runner = FakeRunner()
    adapter = LarkCliIngestionClient(LarkCliBaseClient(_profile(), runner=runner))
    arguments = {
        "base_token": TOKEN,
        "table_id": "tblSeed123",
        "view_id": "vewPending123",
        "field_ids": ("fldQuestion123",),
        "offset": 0,
        "limit": 100,
    }
    arguments.update(overrides)
    with pytest.raises(UnregisteredResourceError) as captured:
        adapter.list_records(**arguments)
    assert TOKEN not in str(captured.value)
    assert "unregistered-token" not in str(captured.value)
    assert runner.calls == []


def test_ingestion_adapter_translates_cli_rate_limit_for_bounded_retry() -> None:
    runner = FakeRunner(_completed("", returncode=1, stderr="HTTP 429 too many requests"))
    adapter = LarkCliIngestionClient(LarkCliBaseClient(_profile(), runner=runner))
    with pytest.raises(BaseRequestError) as captured:
        adapter.list_records(
            base_token=TOKEN,
            table_id="tblSeed123",
            view_id=None,
            field_ids=("fldQuestion123",),
            offset=0,
            limit=100,
        )
    assert captured.value.status_code == 429
    assert TOKEN not in str(captured.value)
