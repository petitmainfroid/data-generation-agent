from __future__ import annotations

import json
import subprocess

import pytest

from data_generation_agent.feishu.client import LarkCliBaseClient
from data_generation_agent.feishu.profile import BaseProfile
from data_generation_agent.feishu.writer import (
    FieldRule,
    FeishuWriteConflictError,
    FeishuWritePolicyError,
    LarkCliBaseWriter,
    _cell_matches,
)


TOKEN = "writer-local-secret-token"


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
                        "sft_id": "fldSft123",
                        "attachment": "fldAttachment123",
                    },
                }
            },
        }
    )


class StatefulRunner:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.calls: list[list[str]] = []
        self.next_id = 1

    def __call__(self, argv, *, timeout: float):
        args = list(argv)
        self.calls.append(args)
        shortcut = args[2]
        if shortcut == "+record-list":
            projection = [args[index + 1] for index, item in enumerate(args) if item == "--field-id"]
            filter_value = json.loads(args[args.index("--filter-json") + 1])
            field_id, _, expected = filter_value["conditions"][0]
            matches = [
                (record_id, fields)
                for record_id, fields in self.records.items()
                if fields.get(field_id) == expected
            ][:2]
            envelope = {
                "ok": True,
                "identity": "user",
                "data": {
                    "data": [[fields.get(field) for field in projection] for _, fields in matches],
                    "record_id_list": [record_id for record_id, _ in matches],
                    "field_id_list": projection,
                    "fields": [{"id": field} for field in projection],
                    "has_more": False,
                },
            }
        elif shortcut == "+record-upsert":
            fields = json.loads(args[args.index("--json") + 1])
            if "--record-id" in args:
                record_id = args[args.index("--record-id") + 1]
            else:
                record_id = f"rec{self.next_id}"
                self.next_id += 1
            self.records.setdefault(record_id, {}).update(fields)
            envelope = {"ok": True, "identity": "user", "data": {"record_id": record_id}}
        else:
            raise AssertionError(f"unexpected shortcut: {shortcut}")
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(envelope), stderr=""
        )


def _writer(runner: StatefulRunner) -> LarkCliBaseWriter:
    profile = _profile()
    reader = LarkCliBaseClient(profile, runner=runner)
    return LarkCliBaseWriter(profile, reader=reader, runner=runner)


def test_business_key_makes_create_and_lost_ack_retry_idempotent() -> None:
    runner = StatefulRunner()
    writer = _writer(runner)
    fields = {"sft_id": "smoke-seed-1", "question": "What is safety stock?"}
    first = writer.upsert("seeds", fields)
    second = writer.upsert("seeds", fields)
    assert first.created is True
    assert second.created is False
    assert first.record_id == second.record_id
    assert first.readback_verified is True
    assert len(runner.records) == 1
    upserts = [call for call in runner.calls if call[2] == "+record-upsert"]
    assert "--record-id" not in upserts[0]
    assert "--record-id" in upserts[1]


def test_unknown_and_non_writable_fields_fail_before_subprocess() -> None:
    runner = StatefulRunner()
    writer = _writer(runner)
    with pytest.raises(FeishuWritePolicyError, match="non-allowlisted"):
        writer.upsert(
            "seeds",
            {"sft_id": "seed-1", "question": "Q", "attachment": []},
        )
    with pytest.raises(FeishuWritePolicyError, match="business"):
        writer.upsert("seeds", {"question": "Q"})
    assert runner.calls == []


def test_duplicate_business_key_and_record_id_conflict_fail_closed() -> None:
    runner = StatefulRunner()
    runner.records = {
        "rec1": {"fldSft123": "duplicate", "fldQuestion123": "Q1"},
        "rec2": {"fldSft123": "duplicate", "fldQuestion123": "Q2"},
    }
    writer = _writer(runner)
    with pytest.raises(FeishuWriteConflictError, match="multiple"):
        writer.upsert("seeds", {"sft_id": "duplicate", "question": "Q"})
    assert not any(call[2] == "+record-upsert" for call in runner.calls)


def test_writer_repr_and_errors_never_contain_profile_token() -> None:
    runner = StatefulRunner()
    writer = _writer(runner)
    assert TOKEN not in repr(writer)

    def failed(argv, *, timeout: float):
        return subprocess.CompletedProcess(
            argv,
            2,
            stdout="",
            stderr=f"base_token={TOKEN} api_key=plain-secret",
        )

    writer.runner = failed
    with pytest.raises(Exception) as captured:
        writer.upsert("seeds", {"sft_id": "seed-1", "question": "Q"})
    assert TOKEN not in str(captured.value)
    assert "plain-secret" not in str(captured.value)


def test_readback_normalizes_single_select_and_numeric_cells() -> None:
    assert _cell_matches(FieldRule("select"), "GENERATE", ["GENERATE"])
    assert _cell_matches(FieldRule("select"), "GENERATE", [{"name": "GENERATE"}])
    assert _cell_matches(FieldRule("number"), 1, 1.0)
    assert not _cell_matches(FieldRule("select"), "GENERATE", ["AUDIT"])
