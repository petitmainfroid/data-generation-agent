from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    ToolContractError,
    atomic_write_json,
    atomic_write_jsonl,
    default_legacy_root,
    file_hash,
    input_hash,
    load_env_values,
    read_jsonl,
    run_legacy_process,
    stable_result_id,
    utc_now,
    validate_question_rows,
)


TOOL_ID = "lao_difficulty_prescreen"
TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = "difficulty_prescreen_result.v1"
PIPELINE_STAGE = "DIFFICULTY_PRESCREEN"
SUBJECTIVE_QWEN_HARD_THRESHOLD = 40.0


def difficulty_paths(legacy_root: Path) -> tuple[Path, Path, Path]:
    module_dir = legacy_root / "modules" / "03_difficulty_fencneg"
    script = module_dir / "run_6w_openai_flow.py"
    objective_eval = module_dir / "objective_eval.py"
    if not script.is_file():
        raise ToolContractError(f"difficulty legacy entrypoint missing: {script}")
    if not objective_eval.is_file():
        raise ToolContractError(f"objective evaluator missing: {objective_eval}")
    return module_dir, script, objective_eval


def preflight_difficulty(legacy_root: Path) -> dict[str, Any]:
    module_dir, script, objective_eval = difficulty_paths(legacy_root)
    return {
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "legacy_root": str(legacy_root),
        "module_dir": str(module_dir),
        "script_hash": file_hash(script),
        "objective_eval_hash": file_hash(objective_eval),
        "pipeline_stage": PIPELINE_STAGE,
        "metric_type": "single_answer_pairwise_gap_prescreen",
        "passrate_supported": False,
        "default_qwen_route": "judge_gateway",
        "next_stage_on_pass": "CONSISTENCY_REVIEW",
        "status": "READY",
    }


def build_difficulty_command(
    *,
    input_path: Path,
    legacy_output_dir: Path,
    legacy_root: Path,
    env_file: Path,
    limit: int = 0,
    answer_max_tokens: int = 32768,
    judge_max_tokens: int = 32768,
    concurrency: int = 1,
    timeout: int = 900,
    stalled_timeout: int = 900,
    wall_timeout: int = 1800,
    objective_threshold: int = 15,
    gt_model: str = "gpt-5.5",
    qwen_model: str = "qwen3.5-35b-a3b",
    judge_model: str = "deepseek-v4-flash",
) -> tuple[list[str], Path]:
    module_dir, script, _ = difficulty_paths(legacy_root)
    command = [
        sys.executable,
        str(script),
        "--input",
        str(input_path),
        "--output",
        str(legacy_output_dir),
        "--env",
        str(env_file),
        "--gt-model",
        gt_model,
        "--qwen-model",
        qwen_model,
        "--judge-model",
        judge_model,
        "--answer-max-tokens",
        str(answer_max_tokens),
        "--judge-max-tokens",
        str(judge_max_tokens),
        "--answer-batch-size",
        "1",
        "--gt-answer-concurrency",
        str(concurrency),
        "--qwen-answer-concurrency",
        str(concurrency),
        "--judge-batch-size",
        str(concurrency),
        "--timeout",
        str(timeout),
        "--stalled-timeout",
        str(stalled_timeout),
        "--answer-wall-timeout",
        str(wall_timeout),
        "--threshold",
        str(objective_threshold),
        "--retry-errors",
    ]
    if limit > 0:
        command.extend(["--limit", str(limit)])
    return command, module_dir


def qwen_gateway_overrides(env_file: Path) -> dict[str, str]:
    values = load_env_values(env_file)
    missing = [key for key in ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY") if not values.get(key)]
    if missing:
        raise ToolContractError(
            "cannot route Qwen through the judge gateway; missing env keys: " + ", ".join(missing)
        )
    return {
        "QWEN_BASE_URL": values["DEEPSEEK_BASE_URL"],
        "QWEN_API_KEY": values["DEEPSEEK_API_KEY"],
    }


def qwen_routing_overrides(env_file: Path, *, qwen_direct: bool) -> dict[str, str]:
    """Return secret in-memory aliases for the selected Qwen route.

    The validated judge gateway is the temporary default. Direct routing is an
    explicit opt-out and leaves the legacy QWEN_* environment untouched.
    """

    if qwen_direct:
        return {}
    return qwen_gateway_overrides(env_file)


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolContractError(f"difficulty field must be numeric: {field}")
    number = float(value)
    if number < 0 or number > 100:
        raise ToolContractError(f"difficulty score out of range: {field}={number}")
    return number


def validate_judgement(judgement: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if judgement.get("error"):
        return errors
    difficulty = str(judgement.get("difficulty", "")).lower()
    if difficulty not in {"hard", "easy"}:
        errors.append("invalid_difficulty")
    try:
        gt_score = _number(judgement.get("gemini_score"), "gemini_score")
        qwen_score = _number(judgement.get("qwen_score"), "qwen_score")
        score_diff = _number(judgement.get("score_diff"), "score_diff")
        if score_diff != abs(gt_score - qwen_score):
            errors.append("score_diff_mismatch")
        method = str(judgement.get("judge_method", ""))
        if method == "deepseek_pairwise_quality_gap":
            expected = "hard" if qwen_score < SUBJECTIVE_QWEN_HARD_THRESHOLD else "easy"
            if difficulty != expected:
                errors.append("subjective_threshold_mismatch")
        elif method == "strict_objective_match":
            if qwen_score not in {0.0, 100.0}:
                errors.append("objective_qwen_score_not_binary")
        else:
            errors.append("unknown_judge_method")
    except ToolContractError as exc:
        errors.append(str(exc))
    return errors


def validate_prescreen_result(result: dict[str, Any]) -> list[str]:
    """Validate the normalized fail-closed gate mapping."""

    errors: list[str] = []
    if result.get("pipeline_stage") != PIPELINE_STAGE:
        errors.append("invalid_pipeline_stage")
    if result.get("passrate") is not None:
        errors.append("passrate_must_be_null")

    decision = result.get("decision")
    actual = (
        result.get("status"),
        result.get("gate_decision"),
        result.get("passes_prescreen"),
        result.get("next_stage"),
    )
    expected = {
        "HARD": ("COMPLETED", "PASS", True, "CONSISTENCY_REVIEW"),
        "EASY": ("COMPLETED", "REJECT_TOO_EASY", False, None),
        "ERROR": ("QUARANTINED", "QUARANTINE", False, None),
    }.get(decision)
    if expected is None:
        errors.append("invalid_decision")
    elif actual != expected:
        errors.append("invalid_gate_mapping")

    expected_difficulty = {"HARD": "HARD", "EASY": "EASY", "ERROR": "UNKNOWN"}.get(
        decision
    )
    if result.get("difficulty") != expected_difficulty:
        errors.append("difficulty_decision_mismatch")
    if decision == "EASY" and "TOO_EASY" not in result.get("issue_codes", []):
        errors.append("missing_too_easy_issue_code")
    if decision == "ERROR" and not result.get("error"):
        errors.append("quarantine_requires_error")
    if decision in {"HARD", "EASY"} and result.get("error") is not None:
        errors.append("completed_result_has_error")
    return errors


def compile_difficulty_results(
    *,
    input_path: Path,
    legacy_output_dir: Path,
    normalized_output_dir: Path,
    preflight: dict[str, Any],
    limit: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs = validate_question_rows(input_path)
    if limit > 0:
        inputs = inputs[:limit]
    legacy_rows = read_jsonl(legacy_output_dir / "quality_gap_001.jsonl")
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for row in legacy_rows:
        candidate_id = str(row.get("sft_id", ""))
        if not candidate_id:
            continue
        if candidate_id in by_id:
            duplicate_ids.add(candidate_id)
        by_id[candidate_id] = row

    results: list[dict[str, Any]] = []
    for candidate_id, source in inputs:
        source_hash = input_hash(source)
        result_id = stable_result_id(
            TOOL_ID,
            TOOL_VERSION,
            candidate_id,
            source_hash,
            preflight["script_hash"],
            preflight["objective_eval_hash"],
            preflight.get("runtime_config", {}),
        )
        base = {
            "schema_version": SCHEMA_VERSION,
            "tool_id": TOOL_ID,
            "tool_version": TOOL_VERSION,
            "candidate_id": candidate_id,
            "input_hash": source_hash,
            "result_id": result_id,
            "reviewed_at": utc_now(),
            "pipeline_stage": PIPELINE_STAGE,
            "metric_type": "single_answer_pairwise_gap_prescreen",
            "passrate": None,
            "routing": {
                "qwen": preflight.get("runtime_config", {}).get(
                    "qwen_route", "judge_gateway"
                )
            },
            "legacy_semantics": {
                "objective": "exact_match",
                "subjective": "qwen_score_below_40_is_hard",
            },
            "artifacts": {
                "legacy_root": str(legacy_output_dir),
                "answers": str(legacy_output_dir / "answers_001.jsonl"),
                "judgements": str(legacy_output_dir / "quality_gap_001.jsonl"),
            },
        }
        if candidate_id in duplicate_ids:
            results.append(
                {
                    **base,
                    "status": "QUARANTINED",
                    "decision": "ERROR",
                    "gate_decision": "QUARANTINE",
                    "passes_prescreen": False,
                    "next_stage": None,
                    "difficulty": "UNKNOWN",
                    "method": "UNKNOWN",
                    "gt_score": None,
                    "qwen_score": None,
                    "score_diff": None,
                    "issue_codes": ["DUPLICATE_LEGACY_RESULT"],
                    "reason": "",
                    "model_versions": {},
                    "error": "legacy difficulty output contains duplicate sft_id",
                }
            )
            continue
        legacy = by_id.get(candidate_id)
        if legacy is None:
            results.append(
                {
                    **base,
                    "status": "QUARANTINED",
                    "decision": "ERROR",
                    "gate_decision": "QUARANTINE",
                    "passes_prescreen": False,
                    "next_stage": None,
                    "difficulty": "UNKNOWN",
                    "method": "UNKNOWN",
                    "gt_score": None,
                    "qwen_score": None,
                    "score_diff": None,
                    "issue_codes": ["MISSING_LEGACY_RESULT"],
                    "reason": "",
                    "model_versions": {},
                    "error": "legacy difficulty output did not contain a terminal result",
                }
            )
            continue
        judgement = legacy.get("quality_gap_judgement")
        if not isinstance(judgement, dict):
            judgement = {}
        legacy_error = str(judgement.get("error", "") or "")
        validation_errors = validate_judgement(judgement)
        if legacy_error or validation_errors:
            results.append(
                {
                    **base,
                    "status": "QUARANTINED",
                    "decision": "ERROR",
                    "gate_decision": "QUARANTINE",
                    "passes_prescreen": False,
                    "next_stage": None,
                    "difficulty": "UNKNOWN",
                    "method": str(judgement.get("judge_method", "UNKNOWN") or "UNKNOWN").upper(),
                    "gt_score": judgement.get("gemini_score"),
                    "qwen_score": judgement.get("qwen_score"),
                    "score_diff": judgement.get("score_diff"),
                    "issue_codes": ["LEGACY_DIFFICULTY_ERROR" if legacy_error else "INVALID_DIFFICULTY_RESPONSE"],
                    "reason": str(judgement.get("reason", "") or ""),
                    "model_versions": legacy.get("models", {}),
                    "error": "; ".join(([legacy_error] if legacy_error else []) + validation_errors),
                }
            )
            continue
        difficulty = str(judgement["difficulty"]).upper()
        passes_prescreen = difficulty == "HARD"
        results.append(
            {
                **base,
                "status": "COMPLETED",
                "decision": difficulty,
                "gate_decision": "PASS" if passes_prescreen else "REJECT_TOO_EASY",
                "passes_prescreen": passes_prescreen,
                "next_stage": "CONSISTENCY_REVIEW" if passes_prescreen else None,
                "difficulty": difficulty,
                "method": str(judgement.get("judge_method", "")).upper(),
                "gt_score": judgement["gemini_score"],
                "qwen_score": judgement["qwen_score"],
                "score_diff": judgement["score_diff"],
                "quality_gap_score": judgement.get("quality_score"),
                "issue_codes": [] if passes_prescreen else ["TOO_EASY"],
                "reason": str(judgement.get("reason", "") or ""),
                "question_type": legacy.get("question_type_judgement", {}),
                "model_versions": legacy.get("models", {}),
                "error": None,
            }
        )

    for result in results:
        contract_errors = validate_prescreen_result(result)
        if contract_errors:
            raise ToolContractError(
                f"normalized prescreen result violated its contract for "
                f"{result.get('candidate_id')}: {', '.join(contract_errors)}"
            )

    counts = Counter(result["decision"] for result in results)
    gate_counts = Counter(result["gate_decision"] for result in results)
    summary = {
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "pipeline_stage": PIPELINE_STAGE,
        "metric_type": "single_answer_pairwise_gap_prescreen",
        "passrate_supported": False,
        "total": len(results),
        "hard": counts["HARD"],
        "easy": counts["EASY"],
        "errors": counts["ERROR"],
        "passed_prescreen": gate_counts["PASS"],
        "rejected_too_easy": gate_counts["REJECT_TOO_EASY"],
        "quarantined": gate_counts["QUARANTINE"],
        "all_inputs_terminal": len(results) == len(inputs),
        "complete": counts["ERROR"] == 0 and len(results) == len(inputs),
        "generated_at": utc_now(),
        "legacy_script_hash": preflight["script_hash"],
        "objective_eval_hash": preflight["objective_eval_hash"],
    }
    atomic_write_jsonl(normalized_output_dir / "results.jsonl", results)
    atomic_write_json(normalized_output_dir / "summary.json", summary)
    return results, summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent-safe adapter for the lao GPT5.5/Qwen difficulty prescreen."
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--answer-max-tokens", type=int, default=32768)
    parser.add_argument("--judge-max-tokens", type=int, default=32768)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--stalled-timeout", type=int, default=900)
    parser.add_argument("--wall-timeout", type=int, default=1800)
    parser.add_argument("--objective-threshold", type=int, default=15)
    parser.add_argument("--gt-model", default="gpt-5.5")
    parser.add_argument("--qwen-model", default="qwen3.5-35b-a3b")
    parser.add_argument("--judge-model", default="deepseek-v4-flash")
    routing = parser.add_mutually_exclusive_group()
    routing.add_argument(
        "--qwen-direct",
        action="store_true",
        help="Opt out of the temporary gateway default and use legacy QWEN_* routing.",
    )
    routing.add_argument(
        "--qwen-via-judge-gateway",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    legacy_root = (args.legacy_root or default_legacy_root(__file__)).resolve()
    preflight = preflight_difficulty(legacy_root)
    if args.preflight_only:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if args.input is None or args.output_dir is None:
        raise ToolContractError("--input and --output-dir are required unless --preflight-only is used")
    if not args.skip_run and args.env_file is None:
        raise ToolContractError("--env-file is required for a real legacy difficulty run")
    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    inputs = validate_question_rows(input_path)
    if args.limit > 0:
        inputs = inputs[: args.limit]
    legacy_output_dir = output_dir / "legacy"
    env_file = args.env_file.resolve() if args.env_file else Path("unused.env")
    command, cwd = build_difficulty_command(
        input_path=input_path,
        legacy_output_dir=legacy_output_dir,
        legacy_root=legacy_root,
        env_file=env_file,
        limit=args.limit,
        answer_max_tokens=args.answer_max_tokens,
        judge_max_tokens=args.judge_max_tokens,
        concurrency=args.concurrency,
        timeout=args.timeout,
        stalled_timeout=args.stalled_timeout,
        wall_timeout=args.wall_timeout,
        objective_threshold=args.objective_threshold,
        gt_model=args.gt_model,
        qwen_model=args.qwen_model,
        judge_model=args.judge_model,
    )
    preflight["runtime_config"] = {
        "gt_model": args.gt_model,
        "qwen_model": args.qwen_model,
        "judge_model": args.judge_model,
        "answer_max_tokens": args.answer_max_tokens,
        "judge_max_tokens": args.judge_max_tokens,
        "objective_threshold": args.objective_threshold,
        "qwen_route": "direct" if args.qwen_direct else "judge_gateway",
    }
    if args.dry_run:
        print(json.dumps({"preflight": preflight, "input_rows": len(inputs), "command": command}, ensure_ascii=False, indent=2))
        return 0
    if not args.skip_run:
        overrides = qwen_routing_overrides(env_file, qwen_direct=args.qwen_direct)
        run_legacy_process(
            command,
            cwd=cwd,
            output_dir=output_dir,
            env_file=env_file,
            env_overrides=overrides,
            timeout_seconds=max(args.wall_timeout + 60, args.timeout + 60),
        )
    _, summary = compile_difficulty_results(
        input_path=input_path,
        legacy_output_dir=legacy_output_dir,
        normalized_output_dir=output_dir,
        preflight=preflight,
        limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["complete"] else 2


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except (ToolContractError, OSError) as exc:
        print(f"difficulty prescreen tool error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
