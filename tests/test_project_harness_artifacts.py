from __future__ import annotations

import json
from pathlib import Path

import yaml

from data_generation_agent.tools.difficulty_prescreen import TOOL_ID, TOOL_VERSION


ROOT = Path(__file__).resolve().parents[1]


def _yaml(relative: str) -> dict[str, object]:
    value = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_pipeline_config_is_canonical_and_fail_closed() -> None:
    config = _yaml("configs/question_pipeline.yaml")
    assert config["full_pipeline_enabled"] is False
    assert config["on_missing_stage"] == "BLOCKED_NOT_IMPLEMENTED"
    assert config["on_stage_error"] == "QUARANTINED_TOOL_ERROR"
    stages = config["stages"]
    assert [stage["stage_id"] for stage in stages] == [
        "quality_review",
        "difficulty_prescreen",
        "consistency_review",
        "answer_synthesis",
        "qwen_passrate_review",
        "final_gate",
    ]
    assert [stage["order"] for stage in stages] == [10, 20, 30, 40, 50, 60]
    assert all(stage["enabled"] is True for stage in stages[:5])
    assert stages[5]["enabled"] is False
    passrate_policy = stages[4]["policy"]
    assert passrate_policy["qwen_answer_max_tokens"] == 32768
    assert passrate_policy["judge_concurrency"] == 20
    assert passrate_policy["judge_max_tokens"] != 32768


def test_prescreen_manifest_matches_source_and_is_enabled_after_reverification() -> None:
    manifest = _yaml("tools/lao_difficulty_prescreen/tool.yaml")
    assert manifest["id"] == TOOL_ID
    assert manifest["version"] == TOOL_VERSION
    assert manifest["enabled"] is True
    assert manifest["command"] == "python -m data_generation_agent.tools.difficulty_prescreen"
    assert (ROOT / manifest["input_schema"]).is_file()
    assert (ROOT / manifest["output_schema"]).is_file()
    assert manifest["passrate_supported"] is False
    assert "qwen_passrate_review" in manifest["must_not_substitute_for"]


def test_consistency_manifest_is_implemented_and_enabled() -> None:
    manifest = _yaml("tools/consistency_review/tool.yaml")
    assert manifest["id"] == "consistency_review"
    assert manifest["version"] == "1.0.0"
    assert manifest["enabled"] is True
    assert manifest["command"] == "python -m data_generation_agent.tools.consistency_review"
    assert (ROOT / manifest["input_schema"]).is_file()
    assert (ROOT / manifest["output_schema"]).is_file()


def test_answer_synthesis_manifest_is_implemented_and_32k() -> None:
    manifest = _yaml("tools/answer_synthesis/tool.yaml")
    assert manifest["version"] == "1.0.0" and manifest["enabled"] is True
    assert manifest["answer_max_tokens"] == 32768
    assert (ROOT / manifest["input_schema"]).is_file()
    assert (ROOT / manifest["output_schema"]).is_file()


def test_qwen_passrate_manifest_has_32k_answers_and_20_judges() -> None:
    manifest = _yaml("tools/qwen_passrate_review/tool.yaml")
    assert manifest["version"] == "1.0.0" and manifest["enabled"] is True
    assert manifest["qwen_answer_max_tokens"] == 32768
    assert manifest["judge_concurrency"] == 20
    assert manifest["judge_max_tokens_independent"] is True


def test_placeholder_manifests_are_non_callable() -> None:
    for tool_id in (
        "final_gate",
    ):
        manifest = _yaml(f"tools/{tool_id}/tool.yaml")
        assert manifest["id"] == tool_id
        assert manifest["enabled"] is False
        assert manifest["implementation_status"] == "placeholder"
        assert manifest["command"] is None
        assert manifest["input_schema"] is None
        assert manifest["output_schema"] is None


def test_feature_list_is_resumable_and_dependency_complete() -> None:
    feature_list = json.loads((ROOT / "feature_list.json").read_text(encoding="utf-8"))
    features = feature_list["features"]
    by_id = {feature["id"]: feature for feature in features}
    assert len(by_id) == len(features)
    assert feature_list["current_focus"] in by_id
    assert sum(feature["status"] == "in_progress" for feature in features) <= 1
    for feature in features:
        assert feature["status"] in {"todo", "in_progress", "blocked", "done"}
        assert feature["acceptance"]
        assert feature["verification"]
        assert all(dependency in by_id for dependency in feature["depends_on"])


def test_prd_covers_required_architecture_and_all_modules() -> None:
    prd = (ROOT / "docs" / "PRD.md").read_text(encoding="utf-8")
    for required in (
        "## 5. 关键架构决策",
        "## 7. 当前项目结构与真实完成度",
        "## 8. 数据、状态与记忆设计",
        "## 10. Harness 验证标准",
        "## 13. 待用户提供或确认",
        "## 15. Long-running Agent 交接契约",
        "passrate 永远为 null",
        "full_pipeline_enabled: false",
    ):
        assert required in prd
    for module_number in range(1, 20):
        assert f"### M{module_number:02d}." in prd
