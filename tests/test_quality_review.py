from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_generation_agent.tools.common import ToolContractError, atomic_write_jsonl, validate_question_rows
from data_generation_agent.tools.quality_review import (
    compile_quality_results,
    parse_args,
    preflight_quality,
    run,
    validate_stage1_review,
)


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "laokuoyang"


def _question(candidate_id: str) -> dict[str, str]:
    return {"sft_id": candidate_id, "question": f"synthetic question {candidate_id}"}


def _logical_review(*, accepted: bool) -> dict[str, object]:
    return {
        "supply_chain_relevance": 1,
        "question_quality_score": 1,
        "non_open_endedness_score": 1,
        "parameter_completeness_score": 1 if accepted else 0,
        "score_rationales": {
            "supply_chain_relevance": "ok",
            "question_quality": "ok",
            "non_open_endedness": "ok",
            "parameter_completeness": "ok" if accepted else "missing threshold",
        },
        "final_decision": "ACCEPT" if accepted else "REJECT",
    }


def test_active_quality_prompts_pass_strict_preflight() -> None:
    result = preflight_quality(LEGACY_ROOT)
    assert result["status"] == "READY"
    assert result["prompt_count"] == 6
    assert all(result["prompt_hashes"].values())


def test_quality_compiler_normalizes_all_terminal_cases(tmp_path: Path) -> None:
    input_path = tmp_path / "questions.jsonl"
    atomic_write_jsonl(
        input_path,
        [_question("accepted"), _question("rejected"), _question("conflict"), _question("missing")],
    )
    legacy = tmp_path / "legacy"
    accepted_review = _logical_review(accepted=True)
    atomic_write_jsonl(
        legacy / "final" / "accepted_dedup.jsonl",
        [
            {
                "row_id": "accepted",
                "question": "synthetic question accepted",
                "problem_category_keys": ["logical_reasoning"],
                "stage1_reviews": {"logical_reasoning": accepted_review},
            },
            {
                "row_id": "conflict",
                "question": "synthetic question conflict",
                "problem_category_keys": ["logical_reasoning"],
                "stage1_reviews": {"logical_reasoning": accepted_review},
            },
        ],
    )
    atomic_write_jsonl(
        legacy / "final" / "rejected_all.jsonl",
        [
            {
                "row_id": "rejected",
                "question": "synthetic question rejected",
                "reject_stage": "stage1",
                "category": "logical_reasoning",
                "review": _logical_review(accepted=False),
                "error": "",
            },
            {
                "row_id": "conflict",
                "question": "synthetic question conflict",
                "reject_stage": "stage1",
                "category": "logical_reasoning",
                "review": _logical_review(accepted=False),
                "error": "",
            },
        ],
    )
    preflight = preflight_quality(LEGACY_ROOT)
    preflight["models"] = {"stage0": "fake-router", "stage1": "fake-reviewer"}
    results, summary = compile_quality_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "normalized",
        preflight=preflight,
    )
    by_id = {row["candidate_id"]: row for row in results}
    assert by_id["accepted"]["decision"] == "ACCEPT"
    assert by_id["accepted"]["result_id"].startswith("lao_quality_review:")
    assert by_id["rejected"]["decision"] == "REJECT"
    assert "MISSING_REQUIRED_CONTEXT" in by_id["rejected"]["issue_codes"]
    assert by_id["conflict"]["issue_codes"] == ["CONFLICTING_LEGACY_TERMINAL"]
    assert by_id["missing"]["issue_codes"] == ["MISSING_LEGACY_RESULT"]
    assert summary == {
        **summary,
        "total": 4,
        "accepted": 1,
        "rejected": 1,
        "errors": 2,
        "complete": False,
    }
    written = [json.loads(line) for line in (tmp_path / "normalized" / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(written) == 4
    first_ids = [row["result_id"] for row in results]
    rerun, _ = compile_quality_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "normalized-rerun",
        preflight=preflight,
    )
    assert [row["result_id"] for row in rerun] == first_ids


def test_quality_response_rejects_score_decision_mismatch() -> None:
    review = _logical_review(accepted=False)
    review["final_decision"] = "ACCEPT"
    assert "decision_score_mismatch:ACCEPT!=REJECT" in validate_stage1_review(review)


def test_quality_compiler_rejects_stale_same_id_result(tmp_path: Path) -> None:
    input_path = tmp_path / "questions.jsonl"
    atomic_write_jsonl(input_path, [_question("same")])
    legacy = tmp_path / "legacy"
    atomic_write_jsonl(
        legacy / "final" / "accepted_dedup.jsonl",
        [
            {
                "row_id": "same",
                "question": "an older question with the same id",
                "problem_category_keys": ["logical_reasoning"],
                "stage1_reviews": {"logical_reasoning": _logical_review(accepted=True)},
            }
        ],
    )
    results, summary = compile_quality_results(
        input_path=input_path,
        legacy_output_dir=legacy,
        normalized_output_dir=tmp_path / "normalized",
        preflight=preflight_quality(LEGACY_ROOT),
    )
    assert results[0]["decision"] == "ERROR"
    assert results[0]["issue_codes"] == ["STALE_LEGACY_RESULT"]
    assert summary["complete"] is False


def test_duplicate_candidate_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.jsonl"
    atomic_write_jsonl(path, [_question("same"), _question("same")])
    with pytest.raises(ToolContractError, match="duplicate candidate id"):
        validate_question_rows(path)


def test_quality_dry_run_has_no_output_side_effect(tmp_path: Path) -> None:
    input_path = tmp_path / "questions.jsonl"
    output_dir = tmp_path / "must-not-exist"
    atomic_write_jsonl(input_path, [_question("dry")])
    args = parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--legacy-root",
            str(LEGACY_ROOT),
            "--dry-run",
        ]
    )
    assert run(args) == 0
    assert not output_dir.exists()
