from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_generation_agent.feishu.profile import (
    BaseProfile,
    BaseProfileError,
    UnregisteredResourceError,
)


TOKEN = "bascn-super-secret-local-token"


def _profile_mapping() -> dict[str, object]:
    return {
        "schema_version": "feishu_base_profile.v1",
        "base_alias": "development",
        "environment": "development",
        "identity": "user",
        "base_token": TOKEN,
        "base_url": "https://example.feishu.cn/base/redacted",
        "tables": {
            "seeds": {
                "table_id": "tblSeed123",
                "name": "种子题",
                "user_entry_field": "question",
                "fields": {
                    "question": "fldQuestion123",
                    "status": "fldStatus123",
                },
                "views": {"pending": "vewPending123"},
            },
            "results": {
                "table_id": "tblResult123",
                "name": "候选结果",
                "fields": {"decision": "fldDecision123"},
            },
        },
    }


def test_profile_loads_alias_allowlists_and_hides_token(tmp_path: Path) -> None:
    path = tmp_path / "profile.local.json"
    path.write_text(json.dumps(_profile_mapping(), ensure_ascii=False), encoding="utf-8")

    profile = BaseProfile.load(path)
    assert profile.base_token == TOKEN
    assert profile.alias == "development"
    assert profile.identity == "user"
    assert profile.table("seeds").table_id == "tblSeed123"
    assert profile.field_id("seeds", "question") == "fldQuestion123"
    assert profile.view_id("seeds", "pending") == "vewPending123"
    assert profile.table("seeds").user_entry_field == "question"
    assert TOKEN not in repr(profile)
    assert TOKEN not in repr(profile.table("seeds"))
    assert "question" in repr(profile.table("seeds"))


def test_unregistered_base_table_field_and_view_fail_closed_without_token() -> None:
    profile = BaseProfile.from_mapping(_profile_mapping())
    calls = (
        lambda: profile.require_base("production"),
        lambda: profile.table("unknown"),
        lambda: profile.field_id("seeds", "unknown"),
        lambda: profile.view_id("seeds", "unknown"),
        lambda: profile.view_id("results", "pending"),
    )
    for call in calls:
        with pytest.raises(UnregisteredResourceError) as captured:
            call()
        assert TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schema_version="unknown"), "schema_version"),
        (lambda value: value.update(identity="admin"), "identity"),
        (
            lambda value: value["tables"]["seeds"].update(table_id="not-a-table-id"),
            "registered ID",
        ),
        (
            lambda value: value["tables"]["seeds"]["fields"].update(question="not-a-field-id"),
            "registered ID",
        ),
        (
            lambda value: value["tables"]["seeds"]["views"].update(pending="not-a-view-id"),
            "registered ID",
        ),
        (
            lambda value: value["tables"]["seeds"].update(user_entry_field="not_registered"),
            "user_entry_field",
        ),
    ],
)
def test_invalid_profile_shapes_do_not_echo_token(mutation, message: str) -> None:
    value = _profile_mapping()
    mutation(value)
    with pytest.raises(BaseProfileError, match=message) as captured:
        BaseProfile.from_mapping(value)
    assert TOKEN not in str(captured.value)


def test_malformed_profile_file_does_not_echo_contents(tmp_path: Path) -> None:
    path = tmp_path / "broken.local.json"
    path.write_text('{"base_token":"' + TOKEN + '", broken', encoding="utf-8")
    with pytest.raises(BaseProfileError) as captured:
        BaseProfile.load(path)
    assert TOKEN not in str(captured.value)
    assert "broken.local.json" in str(captured.value)


def test_registered_maps_are_immutable() -> None:
    profile = BaseProfile.from_mapping(_profile_mapping())
    with pytest.raises(TypeError):
        profile.tables["other"] = profile.table("seeds")  # type: ignore[index]
    with pytest.raises(TypeError):
        profile.table("seeds").fields["other"] = "fldOther123"  # type: ignore[index]
