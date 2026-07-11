from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from data_generation_agent.tools.common import ToolContractError, atomic_write_jsonl
from data_generation_agent.tools.difficulty_prescreen import (
    compile_difficulty_results,
    parse_args,
    preflight_difficulty,
    qwen_gateway_overrides,
    qwen_routing_overrides,
    run,
    validate_judgement,
    validate_prescreen_result,
)


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "laokuoyang"


def _question(candidate_id: str) -> dict[str, str]:
    return {"sft_id": candidate_id, "question": f"synthetic question {candidate_id}"}


def _legacy_row(candidate_id: str, *, qwen_score: int, difficulty: str) -> dict[str, object]:
    gt_score = 90
    return {
        "sft_id": candidate_id,
        "question": f"synthetic question {candidate_id}",
        "models": {"gpt5_5": "gpt-5.5", "qwen": "qwen-test"},
        "question_type_judgement": {"question_form": "short_answer", "is_objective": False},
        "quality_gap_judgement": {
            "gemini_score": gt_score,
            "qwen_score": qwen_score,
            "score_diff": abs(gt_score - qwen_score),
            "difficulty": difficulty,
            "judge_method": "deepseek_pairwise_quality_gap",
            "quality_score": abs(gt_score - qwen_score),
            "reason": "synthetic",
            "error": "",
        },
    }


def test_difficulty_preflight_identifies_prescreen_not_passrate() -> None:
    result = preflight_difficulty(LEGACY_ROOT)
    assert result["status"] == "READY"
    assert result["pipeline_stage"] == "DIFFICULTY_PRESCREEN"
    assert result["metric_type"] == "single_answer_pairwise_gap_prescreen"
    assert result["passrate_supported"] is False
    assert result["default_qwen_route"] == "judge_gateway"
    assert result["next_stage_on_pass"] == "CONSISTENCY_REVIEW"


def test_qwen_gateway_override_uses_aliases_without_changing_source_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_BASE_URL=https://gateway.example/v1\n"
        "DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )
    overrides = qwen_gateway_overrides(env_file)
    assert overrides == {
        "QWEN_BASE_URL": "https://gateway.example/v1",
        "QWEN_API_KEY": "secret-value",
    }
    assert "QWEN_BASE_URL" not in env_file.read_text(encoding="utf-8")


def test_qwen_gateway_is_default_and_direct_is_explicit_opt_out(tmp_path: Path) -> None:
    args = parse_args([])
    assert args.qwen_direct is False
    assert qwen_routing_overrides(tmp_path / "missing.env", qwen_direct=True) == {}

    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_BASE_URL=https://gateway.example/v1\n", encoding="utf-8")
    with pytest.raises(ToolContractError, match="DEEPSEEK_API_KEY"):
        qwen_routing_overrides(env_file, qwen_direct=False)


def test_gateway_and_direct_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--qwen-direct", "--qwen-via-judge-gateway"])


def test_subjective_threshold_boundary_is_validated() -> None:
    hard = _legacy_row("hard", qwen_score=39, difficulty="hard")["quality_gap_judgement"]
    easy = _legacy_row("easy", qwen_score=40, difficulty="easy")["quality_gap_judgement"]
    wrong = _legacy_row("wrong", qwen_score=40, difficulty="hard")["quality_gap_judgement"]
    assert validate_judgement(hard) == []
    assert validate_judgement(easy) == []
    assert "subjective_threshold_mismatch" in validate_judgement(wrong)


def test_prescreen_compiler_normalizes_gates_and_quarantines_bad_outputs(tmp_path: Path) -> None:
    input_path = tmp_path / "questions.jsonl"
    atomic_write_jsonl(
        input_path,
        [_question("hard"), _question("easy"), _question("bad"), _question("duplicate"), _question("missing")],
    )
    legacy = tmp_path / "legacy"
    bad = _legacy_row("bad", qwen_score=39, difficulty="hard")
    bad["quality_gap_judgement"]["qwen_score"] = 139
    rows = [
        _legacy_row("hard", qwen_score=39, difficulty="hard"),
        _legacy_row("easy", qwen_score=40, difficulty="easy"),
        bad,
        _legacy_row("duplicate", qwen_score=39, difficulty="hard"),
        _legacy_row("duplicate", qwen_score=39, difficulty="hard"),
    ]
    atomic_write_jsonl(legacy / "quality_gap_001.jsonl", rows)
    results, summary = compile_difficulty_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "normalized",
        preflight=preflight_difficulty(LEGACY_ROOT),
    )
    by_id = {row["candidate_id"]: row for row in results}

    assert by_id["hard"]["decision"] == "HARD"
    assert by_id["hard"]["gate_decision"] == "PASS"
    assert by_id["hard"]["passes_prescreen"] is True
    assert by_id["hard"]["next_stage"] == "CONSISTENCY_REVIEW"
    assert by_id["hard"]["result_id"].startswith("lao_difficulty_prescreen:")
    assert by_id["hard"]["passrate"] is None

    assert by_id["easy"]["decision"] == "EASY"
    assert by_id["easy"]["gate_decision"] == "REJECT_TOO_EASY"
    assert by_id["easy"]["passes_prescreen"] is False
    assert by_id["easy"]["next_stage"] is None
    assert by_id["easy"]["issue_codes"] == ["TOO_EASY"]
    assert by_id["easy"]["passrate"] is None

    assert by_id["bad"]["issue_codes"] == ["INVALID_DIFFICULTY_RESPONSE"]
    assert by_id["bad"]["status"] == "QUARANTINED"
    assert by_id["bad"]["gate_decision"] == "QUARANTINE"
    assert by_id["bad"]["passrate"] is None
    assert by_id["duplicate"]["issue_codes"] == ["DUPLICATE_LEGACY_RESULT"]
    assert by_id["missing"]["issue_codes"] == ["MISSING_LEGACY_RESULT"]

    assert summary["hard"] == 1
    assert summary["easy"] == 1
    assert summary["errors"] == 3
    assert summary["passed_prescreen"] == 1
    assert summary["rejected_too_easy"] == 1
    assert summary["quarantined"] == 3
    assert summary["all_inputs_terminal"] is True
    assert summary["complete"] is False

    first_ids = [row["result_id"] for row in results]
    rerun, _ = compile_difficulty_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "normalized-rerun",
        preflight=preflight_difficulty(LEGACY_ROOT),
    )
    assert [row["result_id"] for row in rerun] == first_ids


def test_normalized_contract_rejects_gate_mapping_drift() -> None:
    result = {
        "pipeline_stage": "DIFFICULTY_PRESCREEN",
        "passrate": None,
        "status": "COMPLETED",
        "decision": "HARD",
        "gate_decision": "REJECT_TOO_EASY",
        "passes_prescreen": False,
        "next_stage": None,
        "difficulty": "HARD",
        "issue_codes": [],
        "error": None,
    }
    assert "invalid_gate_mapping" in validate_prescreen_result(result)


def test_prescreen_compiler_rejects_stale_same_id_result(tmp_path: Path) -> None:
    input_path = tmp_path / "questions.jsonl"
    legacy = tmp_path / "legacy"
    atomic_write_jsonl(input_path, [_question("same")])
    stale = _legacy_row("same", qwen_score=39, difficulty="hard")
    stale["question"] = "an older question with the same id"
    atomic_write_jsonl(legacy / "quality_gap_001.jsonl", [stale])
    results, summary = compile_difficulty_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "normalized",
        preflight=preflight_difficulty(LEGACY_ROOT),
    )
    assert results[0]["decision"] == "ERROR"
    assert results[0]["gate_decision"] == "QUARANTINE"
    assert results[0]["issue_codes"] == ["STALE_LEGACY_RESULT"]
    assert summary["complete"] is False


def test_runtime_route_changes_result_identity(tmp_path: Path) -> None:
    input_path = tmp_path / "questions.jsonl"
    legacy = tmp_path / "legacy"
    atomic_write_jsonl(input_path, [_question("hard")])
    atomic_write_jsonl(
        legacy / "quality_gap_001.jsonl",
        [_legacy_row("hard", qwen_score=39, difficulty="hard")],
    )
    gateway = preflight_difficulty(LEGACY_ROOT)
    gateway["runtime_config"] = {"qwen_route": "judge_gateway"}
    direct = preflight_difficulty(LEGACY_ROOT)
    direct["runtime_config"] = {"qwen_route": "direct"}
    gateway_rows, _ = compile_difficulty_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "gateway",
        preflight=gateway,
    )
    direct_rows, _ = compile_difficulty_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "direct",
        preflight=direct,
    )
    assert gateway_rows[0]["routing"] == {"qwen": "judge_gateway"}
    assert direct_rows[0]["routing"] == {"qwen": "direct"}
    assert gateway_rows[0]["result_id"] != direct_rows[0]["result_id"]


def test_dry_run_exposes_route_without_secret_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    input_path = tmp_path / "questions.jsonl"
    output_dir = tmp_path / "must-not-exist"
    env_file = tmp_path / ".env"
    atomic_write_jsonl(input_path, [_question("dry")])
    env_file.write_text(
        "DEEPSEEK_BASE_URL=https://gateway.example/v1\n"
        "DEEPSEEK_API_KEY=do-not-print-this-secret\n",
        encoding="utf-8",
    )
    args = parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--legacy-root",
            str(LEGACY_ROOT),
            "--env-file",
            str(env_file),
            "--dry-run",
        ]
    )
    assert run(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["preflight"]["runtime_config"]["qwen_route"] == "judge_gateway"
    assert "do-not-print-this-secret" not in output
    assert not output_dir.exists()


def test_schema_declares_fail_closed_prescreen_mapping() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "difficulty_prescreen_result.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["tool_id"]["const"] == "lao_difficulty_prescreen"
    assert schema["properties"]["passrate"]["type"] == "null"
    assert schema["properties"]["gate_decision"]["enum"] == [
        "PASS",
        "REJECT_TOO_EASY",
        "QUARANTINE",
    ]
    assert len(schema["allOf"]) == 3


def test_legacy_objective_exact_match_semantics() -> None:
    path = LEGACY_ROOT / "modules" / "03_difficulty_fencneg" / "objective_eval.py"
    spec = importlib.util.spec_from_file_location("lao_objective_eval_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    type_judgement = {
        "question_form": "single_choice",
        "is_objective": True,
        "objective_kind": "choice",
        "answer_extraction_present": True,
        "standard_answer": "A",
        "model_answer": "A",
        "confidence": 1.0,
        "reason": "fixture",
        "error": "",
    }
    record = {
        "sft_id": "objective",
        "question": "A or B",
        "gpt5_5": {"answer": "A"},
        "qwen": {"answer": "A"},
    }
    matched = module.build_strict_objective_judgement(record, type_judgement, 15)
    assert matched["quality_gap_judgement"]["difficulty"] == "easy"
    type_judgement["model_answer"] = "B"
    mismatched = module.build_strict_objective_judgement(record, type_judgement, 15)
    assert mismatched["quality_gap_judgement"]["difficulty"] == "hard"
    assert mismatched["quality_gap_judgement"]["qwen_score"] == 0
